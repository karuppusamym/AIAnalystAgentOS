"""General entity matching (INT-004, N-7): propose links between two tables, review them, promote a crosswalk.

* **Run.** A `MatchSpec` names two assets of the caller's scope (the same or different sources), their record
  keys and the fields to compare. Each side is read once through `QueryGateway.execute` under the caller's
  scope and that source's identity, only the named columns, bounded by the spec's row cap (a larger table
  is refused, never sampled) and with `retain_rows=False`, so no value lands in the audit preview or the
  result cache. The rows live only in this process while `skills/entity_matching.py` turns them into
  features; a PII-tagged column (catalog tag or the workspace policy's `pii_columns`) is hashed with the
  workspace key at that moment. A column the caller may not read (PII without clearance, restricted) is
  refused before any read, and the gateway would refuse it again. No model is involved at any step.
* **Proposals.** Every pair at or above the review threshold is stored with its score, band and per-field
  similarities (numbers only; a record key can never be a PII column). Nothing is linked yet.
* **Review.** An editor accepts or rejects pairs one by one, or a whole band at once (``accept_band``).
* **Promote.** Once no pair is undecided, the accepted pairs are the crosswalk. Promotion is a hash-bound
  approval (`entity_match.promote`: run, spec, both input versions, the crosswalk hash, the destination);
  `verify_for_execution` runs immediately before the managed writer loads the crosswalk into the workspace's
  managed output source. The run then carries the two reviewed join keys (crosswalk.left_key -> left key,
  crosswalk.right_key -> right key) as `origin="user"` proposals for `skills/federation.validate_join_keys`,
  which still measures containment and cardinality before analysis joins across them.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError, Conflict, Forbidden, InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import EntityMatchPair, EntityMatchRun, Source, SourceAsset, SourceColumn, User, Workspace
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, load_policy, require_role, resolve_scope, scoped_loader
from analystos.skills import entity_matching as em

ACTION = "entity_match.promote"
SUBJECT = "entity_match"
CROSSWALK_COLUMNS = ["left_key", "right_key", "match_score", "match_band", "reviewed_by", "match_run_id"]
DECISIONS = {"accept": "accepted", "reject": "rejected"}


# ------------------------------------------------------------------------------------ checks (pure)
def parse_spec(spec: dict[str, Any] | em.MatchSpec) -> em.MatchSpec:
    if isinstance(spec, em.MatchSpec):
        return spec
    try:
        return em.MatchSpec.model_validate(spec)
    except ValidationError as exc:
        raise InvalidInput("invalid match spec: " + "; ".join(e["msg"] for e in exc.errors()[:5]),
                           details={"errors": exc.errors(include_url=False, include_context=False)}) from None


def _pattern(pattern: str, fq: str) -> bool:
    from analystos.governance.policy import _matches

    return _matches(pattern, fq)


def pii_columns(asset: str, tags: dict[str, list[str] | None], policy_patterns: list[str]) -> set[str]:
    """Columns of `asset` handled as PII: tagged `pii` in the catalog or named by the workspace policy."""
    return {name for name, t in tags.items()
            if "pii" in set(t or []) or any(_pattern(p, f"{asset}.{name}") for p in policy_patterns)}


def check_spec(spec: em.MatchSpec, scope: Any, pii: dict[str, set[str]]) -> None:
    """Refuse before any read: an asset outside the scope (or in another source than named), an unknown
    column, a column the caller may not read, and a PII record key (it would be copied into the crosswalk)."""
    for side, columns in ((spec.left, spec.left_columns()), (spec.right, spec.right_columns())):
        if side.asset not in scope.assets:
            raise Forbidden(f"{side.asset} is not in the authorized scope")
        if side.source_id and scope.asset_sources.get(side.asset) != side.source_id:
            raise Forbidden(f"{side.asset} is not an asset of source {side.source_id}")
        known = set(scope.columns.get(side.asset) or [])
        missing = [c for c in columns if c not in known]
        if missing:
            raise InvalidInput(f"{side.asset} has no column {', '.join(missing)}", details={"missing": missing})
        denied = set(scope.denied_columns)
        blocked = [c for c in columns if f"{side.asset}.{c}" in denied or f"*.{c}" in denied]
        if blocked:
            raise Forbidden(f"column {', '.join(blocked)} of {side.asset} is not readable under the workspace policy "
                            "(PII without clearance, or restricted)", details={"columns": blocked})
        if side.key in pii.get(side.asset, set()):
            raise InvalidInput(f"the record key {side.asset}.{side.key} is PII; a crosswalk would copy it. Use a "
                               "non-PII identifier as the key and compare the PII column as a field")


def read_side(runner: Callable[..., Any], side: em.MatchSide, columns: list[str], *, max_rows: int,
              purpose: str) -> tuple[list[dict[str, Any]], str]:
    """The named columns of one side, read once through the gateway runner; a truncated read is refused."""
    from sqlglot import exp

    from analystos.skills.sqlbuild import col, table, to_sql

    stmt = exp.select(*[col(c) for c in columns]).from_(table(side.asset))
    res = runner(to_sql(stmt, getattr(runner, "dialect", "postgres")), purpose=purpose, max_rows=max_rows,
                 retain_rows=False, use_cache=False)
    if res.truncated:
        raise InvalidInput(f"{side.asset} has more rows than the match row cap ({max_rows}, or the workspace's query "
                           "cap); a sample would silently miss links, so nothing was matched. Filter it first (a recipe)")
    return res.records(), res.query_id


def crosswalk(pairs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Accepted pairs in key order and their hash. Each key may appear once on each side: a crosswalk that
    maps a record twice would multiply rows in every join through it."""
    rows = sorted(({"left_key": p["left_key"], "right_key": p["right_key"], "score": p["score"], "band": p["band"],
                    "decided_by": p["decided_by"]} for p in pairs), key=lambda r: (r["left_key"], r["right_key"]))
    for side in ("left_key", "right_key"):
        seen: set[str] = set()
        dupes = sorted({r[side] for r in rows if r[side] in seen or seen.add(r[side])})
        if dupes:
            raise Conflict(f"{len(dupes)} {side.split('_')[0]} record(s) are accepted in more than one pair (e.g. "
                           f"{dupes[0]}); reject all but one so the crosswalk stays one-to-one",
                           details={"side": side, "keys": dupes[:20]})
    return rows, stable_hash([[r["left_key"], r["right_key"]] for r in rows])


def promotion_payload(run: EntityMatchRun, crosswalk_hash: str, rows: int, table: str) -> dict[str, Any]:
    return {"action": ACTION, "run_id": run.id, "spec_hash": run.spec_hash, "left_asset": run.left_asset,
            "right_asset": run.right_asset, "left_version": run.left_version, "right_version": run.right_version,
            "crosswalk_hash": crosswalk_hash, "rows": rows, "table": table}


def join_keys(spec: em.MatchSpec, crosswalk_asset: str) -> list[dict[str, Any]]:
    """The reviewed join keys: the crosswalk references both sides (each of its keys points at a unique record
    key), as proposals the federation validator still measures for containment and cardinality."""
    from analystos.skills.federation import JoinProposal

    return [JoinProposal(from_asset=crosswalk_asset, from_column=col, to_asset=side.asset, to_column=side.key,
                         origin="user", rule="entity_match").model_dump()
            for col, side in (("left_key", spec.left), ("right_key", spec.right))]


# ------------------------------------------------------------------------------------ views
def run_view(row: EntityMatchRun) -> dict[str, Any]:
    return {k: getattr(row, k) for k in ("id", "workspace_id", "name", "spec", "spec_hash", "left_asset", "right_asset",
                                         "left_source_id", "right_source_id", "left_version", "right_version",
                                         "pii_fields", "status", "stats", "query_ids", "approval_id", "crosswalk_hash",
                                         "crosswalk_source_id", "crosswalk_table", "join_keys", "error", "created_by",
                                         "created_at", "finished_at", "promoted_at")}


def pair_view(row: EntityMatchPair) -> dict[str, Any]:
    return {k: getattr(row, k) for k in ("id", "run_id", "left_key", "right_key", "score", "band", "fields", "decision",
                                         "decided_by", "decided_at", "note")}


def _decision_counts(session: Session, run_id: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for band, decision, n in session.execute(select(EntityMatchPair.band, EntityMatchPair.decision, func.count())
                                             .where(EntityMatchPair.run_id == run_id)
                                             .group_by(EntityMatchPair.band, EntityMatchPair.decision)):
        out.setdefault(band, {})[decision] = int(n)
    return out


# ------------------------------------------------------------------------------------ run
def _catalog_pii(session: Session, workspace_id: str, scope: Any, assets: list[str]) -> dict[str, set[str]]:
    policy = load_policy(session, session.get(Workspace, workspace_id))
    out: dict[str, set[str]] = {}
    for fq in assets:
        schema, name = fq.split(".", 1)
        q = (select(SourceColumn.name, SourceColumn.tags).join(SourceAsset, SourceAsset.id == SourceColumn.asset_id)
             .where(SourceAsset.workspace_id == workspace_id, SourceAsset.schema_name == schema, SourceAsset.name == name))
        if scope.asset_sources.get(fq):
            q = q.where(SourceAsset.source_id == scope.asset_sources[fq])
        out[fq] = pii_columns(fq, dict(session.execute(q).all()), policy.pii_columns)
    return out


def _runner_for(user: User, scope: Any) -> Callable[[str], Any]:
    from analystos.runtime.context import default_gateway

    return lambda source_id: default_gateway().run_sql_for(scope, actor=f"user:{user.id}", source_id=source_id)


def _fail(run_id: str, message: str) -> None:
    with session_scope() as s:
        row = s.get(EntityMatchRun, run_id)
        row.status, row.error, row.finished_at = "failed", message[:4000], utcnow()
        emit(row.workspace_id, "entity_match.failed", {"match_run_id": row.id, "error": message[:300]},
             actor=row.created_by, session=s)


def start_match(user: User, workspace_id: str, spec: dict[str, Any] | em.MatchSpec, *, scope: Any = None,
                runner_for: Callable[[str | None], Any] | None = None) -> dict[str, Any]:
    """Read both sides through the gateway, link them deterministically and store the proposals."""
    spec = parse_spec(spec)
    with session_scope() as s:
        require_role(s, user, workspace_id, "analyst")
        scope = scope or resolve_scope(s, s.merge(user), workspace_id, minimum_role="analyst")
        pii = _catalog_pii(s, workspace_id, scope, [spec.left.asset, spec.right.asset])
        check_spec(spec, scope, pii)
        pii_left, pii_right = pii[spec.left.asset], pii[spec.right.asset]
        pii_fields = [f.label for f in spec.fields if f.left in pii_left or f.right in pii_right]
        body = spec.model_dump(mode="json")
        row = EntityMatchRun(id=new_id("emr"), workspace_id=workspace_id, name=spec.name, spec=body,
                             spec_hash=stable_hash(body), left_asset=spec.left.asset, right_asset=spec.right.asset,
                             left_source_id=scope.asset_sources.get(spec.left.asset),
                             right_source_id=scope.asset_sources.get(spec.right.asset), pii_fields=pii_fields,
                             status="running", stats={}, query_ids=[], join_keys=[], created_by=f"user:{user.id}")
        s.add(row)
        s.flush()
        run_id, left_src, right_src = row.id, row.left_source_id, row.right_source_id
        emit(workspace_id, "entity_match.started", {"match_run_id": run_id, "name": spec.name}, actor=f"user:{user.id}",
             session=s)
    try:
        runner_for = runner_for or _runner_for(user, scope)
        left_rows, lq = read_side(runner_for(left_src), spec.left, spec.left_columns(), max_rows=spec.max_rows,
                                  purpose=f"entity_match:{run_id}:left")
        right_rows, rq = read_side(runner_for(right_src), spec.right, spec.right_columns(), max_rows=spec.max_rows,
                                   purpose=f"entity_match:{run_id}:right")
        hasher = em.workspace_hasher(get_settings().jwt_secret, workspace_id) if pii_fields else None
        try:
            result = em.link_records(left_rows, right_rows, spec,
                                     pii_left={f.left for f in spec.fields if f.left in pii_left},
                                     pii_right={f.right for f in spec.fields if f.right in pii_right}, hasher=hasher)
        except ValueError as exc:  # a non-unique key, or blocking too loose for the comparison budget
            raise InvalidInput(str(exc)) from None
        del left_rows, right_rows  # the raw values (PII included) go no further than this process
    except AnalystOSError as exc:
        _fail(run_id, exc.message)
        raise
    except Exception as exc:
        _fail(run_id, f"{type(exc).__name__}: {exc}")
        raise
    stats = dict(result.stats)
    with session_scope() as s:
        from analystos.artifacts.registry import link

        row = s.get(EntityMatchRun, run_id)
        row.left_version, row.right_version = stats.pop("left_version"), stats.pop("right_version")
        row.stats, row.query_ids, row.status, row.finished_at = stats, [lq, rq], "proposed", utcnow()
        s.add_all(EntityMatchPair(id=new_id("emp"), run_id=run_id, workspace_id=workspace_id, left_key=p.left_key,
                                  right_key=p.right_key, score=p.score, band=p.band, fields=p.fields, decision="proposed")
                  for p in result.pairs)
        link(s, workspace_id, ("table", spec.left.asset), "linked_by", (SUBJECT, run_id))
        link(s, workspace_id, ("table", spec.right.asset), "linked_by", (SUBJECT, run_id))
        audit(f"user:{user.id}", "entity_match.proposed", workspace_id=workspace_id, target=run_id,
              details={"name": spec.name, "left": spec.left.asset, "right": spec.right.asset, "pii_fields": pii_fields,
                       "match": stats["match"], "review": stats["review"], "query_ids": [lq, rq]}, session=s)
        emit(workspace_id, "entity_match.proposed", {"match_run_id": run_id, "match": stats["match"],
                                                     "review": stats["review"]}, actor=f"user:{user.id}", session=s)
        return run_view(row)


# ------------------------------------------------------------------------------------ read
@scoped_loader
def get_run(session: Session, user: User, run_id: str, workspace_id: str | None = None, minimum: str = "viewer",
            for_update: bool = False) -> EntityMatchRun:
    return load_in_workspace(session, EntityMatchRun, run_id, workspace_id, user=user, minimum=minimum,
                             label="entity match run", for_update=for_update)


def list_runs(session: Session, user: User, workspace_id: str) -> list[EntityMatchRun]:
    require_role(session, user, workspace_id, "viewer")
    return list(session.scalars(select(EntityMatchRun).where(EntityMatchRun.workspace_id == workspace_id)
                                .order_by(EntityMatchRun.created_at.desc()).limit(200)))


@scoped_loader
def run_detail(session: Session, user: User, run_id: str, workspace_id: str | None = None) -> dict[str, Any]:
    row = get_run(session, user, run_id, workspace_id)
    return {**run_view(row), "decisions": _decision_counts(session, row.id)}


@scoped_loader
def list_pairs(session: Session, user: User, run_id: str, workspace_id: str | None = None, *, band: str | None = None,
               decision: str | None = None, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    row = get_run(session, user, run_id, workspace_id)
    q = select(EntityMatchPair).where(EntityMatchPair.run_id == row.id)
    if band:
        q = q.where(EntityMatchPair.band == band)
    if decision:
        q = q.where(EntityMatchPair.decision == decision)
    total = session.scalar(select(func.count()).select_from(q.subquery())) or 0
    items = session.scalars(q.order_by(EntityMatchPair.score.desc(), EntityMatchPair.left_key, EntityMatchPair.right_key)
                            .limit(limit).offset(offset))
    return {"total": int(total), "pairs": [pair_view(p) for p in items]}


# ------------------------------------------------------------------------------------ review
@scoped_loader
def review(user: User, run_id: str, workspace_id: str | None = None, *, decisions: list[dict[str, Any]] | None = None,
           accept_band: str | None = None, reject_band: str | None = None) -> dict[str, Any]:
    """A person's decisions: per pair, or every still-undecided pair of a band. A change after a promotion
    was requested moves the run back to `proposed` (the requested approval no longer matches the crosswalk)."""
    with session_scope() as s:
        row = get_run(s, user, run_id, workspace_id, minimum="editor", for_update=True)
        if row.status not in ("proposed", "awaiting_approval"):
            raise Conflict(f"entity match run {run_id} is {row.status}; only proposals can be reviewed")
        who, now, changed = f"user:{user.id}", utcnow(), 0
        for band, decision in ((accept_band, "accepted"), (reject_band, "rejected")):
            if band is None:
                continue
            if band not in ("match", "review"):
                raise InvalidInput("a band is `match` or `review`")
            changed += s.execute(update(EntityMatchPair).where(
                EntityMatchPair.run_id == row.id, EntityMatchPair.band == band, EntityMatchPair.decision == "proposed")
                .values(decision=decision, decided_by=who, decided_at=now)
                .execution_options(synchronize_session=False)).rowcount
        for d in decisions or []:
            pair = s.get(EntityMatchPair, d.get("pair_id"))
            if pair is None or pair.run_id != row.id:
                raise NotFound(f"pair {d.get('pair_id')} not found in run {run_id}")
            if d.get("decision") not in DECISIONS:
                raise InvalidInput("a decision is `accept` or `reject`")
            pair.decision, pair.decided_by, pair.decided_at = DECISIONS[d["decision"]], who, now
            pair.note = (d.get("note") or None) and str(d["note"])[:2000]
            changed += 1
        if changed and row.status == "awaiting_approval":
            row.status = "proposed"
        s.flush()
        counts = _decision_counts(s, row.id)
        audit(who, "entity_match.reviewed", workspace_id=row.workspace_id, target=row.id,
              details={"changed": changed, "accept_band": accept_band, "reject_band": reject_band, "decisions": counts},
              session=s)
        emit(row.workspace_id, "entity_match.reviewed", {"match_run_id": row.id, "changed": changed}, actor=who, session=s)
        return {**run_view(row), "decisions": counts}


# ------------------------------------------------------------------------------------ promote
def _crosswalk_table(spec: em.MatchSpec) -> str:
    from analystos.connectors.naming import sanitize_identifier
    from analystos.staging.loader import MAX_TABLE_NAME

    return sanitize_identifier(f"xwalk_{spec.name}", max_length=MAX_TABLE_NAME, fallback="xwalk")


def _accepted(session: Session, run: EntityMatchRun) -> tuple[list[dict[str, Any]], str]:
    undecided = session.scalar(select(func.count()).where(EntityMatchPair.run_id == run.id,
                                                          EntityMatchPair.decision == "proposed")) or 0
    if undecided:
        raise Conflict(f"{undecided} pair(s) are still undecided; accept or reject every proposal (a whole band at "
                       "once with accept_band) before promoting", details={"undecided": int(undecided)})
    pairs = [pair_view(p) for p in session.scalars(select(EntityMatchPair).where(EntityMatchPair.run_id == run.id,
                                                                                 EntityMatchPair.decision == "accepted"))]
    if not pairs:
        raise InvalidInput("no pair was accepted; there is nothing to promote")
    return crosswalk(pairs)


@scoped_loader
def promote(user: User, run_id: str, workspace_id: str | None = None, *, approval_id: str | None = None) -> dict[str, Any]:
    """Without `approval_id`: request the hash-bound `entity_match.promote` approval for the accepted pairs.
    With it: verify the approval against the crosswalk recomputed now, then load the crosswalk into the
    managed output source and record the reviewed join keys."""
    from analystos.governance.approvals import consume, request_approval, verify_for_execution

    with session_scope() as s:
        row = get_run(s, user, run_id, workspace_id, minimum="editor", for_update=True)
        ws = row.workspace_id
        if row.status == "promoted":
            return {"status": "promoted", "match_run": run_view(row)}
        if row.status not in ("proposed", "awaiting_approval"):
            raise Conflict(f"entity match run {run_id} is {row.status}")
        spec = parse_spec(row.spec)
        rows, digest = _accepted(s, row)
        table = _crosswalk_table(spec)
        payload = promotion_payload(row, digest, len(rows), table)
        if not approval_id:
            wsrow = s.get(Workspace, ws)
            apr = request_approval(s, workspace_id=ws, run_id=None, action=ACTION, payload=payload, plan_hash=None,
                                   policy_version=wsrow.policy_version, requested_by=user.id, risk_tier="medium",
                                   destination=f"managed_output:{table}", affected_assets=[row.left_asset, row.right_asset],
                                   evidence={"stats": row.stats, "decisions": _decision_counts(s, row.id),
                                             "pii_fields": row.pii_fields})
            row.status, row.approval_id, row.crosswalk_hash = "awaiting_approval", apr.id, digest
            emit(ws, "entity_match.promotion_requested", {"match_run_id": row.id, "approval_id": apr.id, "rows": len(rows)},
                 actor=f"user:{user.id}", session=s)
            return {"status": "approval_required", "approval_id": apr.id, "payload_hash": apr.payload_hash,
                    "match_run_id": row.id}
        apr = verify_for_execution(s, approval_id, payload=payload, plan_hash=None)
        if apr.action != ACTION or apr.workspace_id != ws:
            raise PolicyDenied(f"approval {approval_id} does not authorize this crosswalk promotion")
        consume(s, apr)
    try:
        written = _write_crosswalk(user, run_id, ws, table, rows)
    except AnalystOSError as exc:
        _fail(run_id, exc.message)
        raise
    with session_scope() as s:
        from analystos.artifacts.registry import link

        row = s.get(EntityMatchRun, run_id)
        row.status, row.promoted_at, row.approval_id, row.crosswalk_hash = "promoted", utcnow(), approval_id, digest
        row.crosswalk_source_id, row.crosswalk_table = written["source_id"], written["table"]
        row.join_keys = join_keys(spec, written["table"])
        link(s, ws, (SUBJECT, row.id), "produced", ("table", written["table"]))
        audit(f"user:{user.id}", "entity_match.promoted", workspace_id=ws, target=row.id, decision="allow",
              details={"approval_id": approval_id, "table": written["table"], "rows": len(rows), "crosswalk_hash": digest},
              session=s)
        emit(ws, "entity_match.promoted", {"match_run_id": row.id, "table": written["table"], "rows": len(rows)},
             actor=f"user:{user.id}", session=s)
        return {"status": "promoted", "match_run": run_view(row)}


def _write_crosswalk(user: User, run_id: str, ws: str, table: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The crosswalk into the workspace's managed output source (ADR-0011), replaced as a whole; it is then
    read like any staged asset, through the gateway, under the workspace reader role."""
    from analystos.services.ml import _arrow
    from analystos.services.recipes import _register_asset, ensure_output_source
    from analystos.staging.loader import StagingLoader

    with session_scope() as s:
        source_id = ensure_output_source(s, ws).id
    data = [[r["left_key"], r["right_key"], float(r["score"]), r["band"], r["decided_by"], run_id] for r in rows]
    info = StagingLoader(get_settings()).load(source_id, table, _arrow(CROSSWALK_COLUMNS, data), workspace_id=ws,
                                              mode="replace", fingerprint="table")
    with session_scope() as s:
        src = s.get(Source, source_id)
        asset = _register_asset(s, src, info, recipe=f"entity_match:{run_id}", output=table, role="output", tags={})
        asset.description = f"Reviewed crosswalk of entity match run {run_id}"
        src.last_discovered_at = utcnow()
    return {"source_id": source_id, "table": f"{info['schema']}.{info['table']}"}
