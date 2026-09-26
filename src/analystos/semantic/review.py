"""Semantic review (P4-05, P7-09): what a person reviews before a definition becomes governed, and what
happens to governed definitions when the structure under them changes.

* **Definition checks at approval** (`definition_problems`, ADR-0019 §5). A metric's expression must parse
  for its declared dialect and read only its dataset's columns; its filters parse and do not aggregate;
  its dimensions exist; a dimension on another dataset needs one validated join path, and an additive
  metric that would cross a fan-out edge must declare that edge in `pre_aggregations`. The same checks
  run offline (`analystos check-semantics`) and against every newly approved structure version.
* **Structure approvals.** An agent-proposed model version (entities, grain = primary keys, joins) is
  approved through `governance/approvals.py`: hash-bound to the version's content, separation of duties
  enforced (the approver is not the accountable requester), `verify_for_execution` before it applies.
* **Relationship review queue.** Candidates are measured through the gateway (skills/relationships:
  containment, each side's uniqueness, the implied cardinality, Atlas's assessment). Accepting one is the
  same kind of approval; it writes the relationship with its measured cardinality and
  `validated_at`/`validated_by`. No request body, model or import can set a cardinality.
* **Stale-definition invalidation.** When a structure version becomes approved, every approved metric is
  re-checked against it; one that no longer holds (dataset or column gone, join no longer validated) is
  deprecated with the reason, and publication refuses it (`service.gate_bundle`).
* **Reconciliation.** `reconcile` reports fan-out and denominator problems, stale and unvalidated
  definitions in one place (with the SEM-005 conflicts).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp

from analystos.contracts.semantic import (
    DialectExpression,
    SemanticDataset,
    SemanticField,
    SemanticMetricDef,
    SemanticRelationship,
)
from analystos.core.errors import Conflict, Forbidden, InvalidInput, NotFound
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import (
    AnalysisRun,
    Approval,
    SemanticModel,
    SemanticRelationshipCandidate,
    SourceAsset,
    SourceColumn,
    User,
    Workspace,
)
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.semantic import ossie

MODEL_APPROVAL_ACTION = "semantic_model.approve"
RELATIONSHIP_APPROVAL_ACTION = "semantic_relationship.accept"
MEASUREMENT_MAX_AGE = timedelta(days=7)  # an older measurement is re-taken before a decision


# ------------------------------------------------------------------------------------ definition checks
def _read(dialect: str) -> str | None:
    return ossie._SQLGLOT[dialect] or None


def _dataset_columns(dataset: dict[str, Any]) -> set[str] | None:
    """Declared field names plus the source's explicit output columns; None when nothing is declared."""
    names = {f["name"].lower() for f in dataset.get("fields") or []}
    try:
        source = sqlglot.parse_one(dataset.get("source") or "", read=None)
    except sqlglot.errors.SqlglotError:
        source = None
    if isinstance(source, exp.Select):
        names |= {p.alias_or_name.lower() for p in source.expressions if not isinstance(p, exp.Star)}
    return names or None


def definition_problems(defn: SemanticMetricDef, datasets: list[dict[str, Any]],
                        relationships: list[dict[str, Any]]) -> list[str]:
    """Why this metric cannot be a governed definition over this structure (empty = it can)."""
    from analystos.semantic.compiler import FANOUT, _additive, join_path

    if defn.dialect not in ossie._SQLGLOT:
        return [f"expression dialect {defn.dialect} is not SQL the compiler can parse"]
    problem = ossie.metric_problem(defn)
    if problem:
        return [f"expression {problem}"]
    read = _read(defn.dialect)
    tree = sqlglot.parse_one(defn.expression, read=read)
    problems, used = [], {c.name.lower() for c in tree.find_all(exp.Column)}
    for f in defn.filters:
        try:
            cond = sqlglot.parse_one(f, read=read)
        except sqlglot.errors.SqlglotError as exc:
            problems.append(f"filter {f!r} does not parse: {str(exc).splitlines()[0]}")
            continue
        if any(isinstance(n, ossie.AGGREGATES + (exp.Select, exp.Subquery)) for n in cond.walk()):
            problems.append(f"filter {f!r} must be a row condition (no aggregates or subqueries)")
        used |= {c.name.lower() for c in cond.find_all(exp.Column)}
    by_name = {d["name"]: d for d in datasets}
    rel_names = {r["name"] for r in relationships}
    for name in defn.pre_aggregations:
        if name not in rel_names:
            problems.append(f"pre_aggregations names {name!r}, which is not a relationship of the model")
    if not defn.dataset:
        return problems
    dataset = by_name.get(defn.dataset)
    if dataset is None:
        return [*problems, f"dataset {defn.dataset!r} is not in the semantic model"]
    columns = _dataset_columns(dataset)
    if columns is None:
        problems.append(f"dataset {defn.dataset!r} declares no fields to check the expression against")
    else:
        unknown = sorted(used - columns)
        if unknown:
            problems.append(f"references {', '.join(unknown)}, which {defn.dataset!r} does not have")
    additive = _additive(tree)
    for dim in defn.dimensions:
        ds_name, _, field = dim.rpartition(".")
        ds_name = ds_name or defn.dataset
        target = by_name.get(ds_name)
        if target is None or field.lower() not in (_dataset_columns(target) or set()):
            problems.append(f"dimension {dim!r} is not a field of the model")
            continue
        if ds_name == defn.dataset:
            continue
        try:
            path = join_path(relationships, defn.dataset, ds_name)
        except InvalidInput as exc:
            problems.append(f"dimension {dim!r}: {exc.message}")
            continue
        for j in path:
            if j.cardinality in FANOUT and additive and j.relationship["name"] not in defn.pre_aggregations:
                problems.append(f"dimension {dim!r} crosses {j.relationship['name']} ({j.from_ds} -> {j.to_ds}, "
                                f"{j.cardinality}); an additive metric needs pre_aggregations: [{j.relationship['name']}]")
    return problems


# ------------------------------------------------------------------------------------ structure approvals
def approved_model(session: Session, workspace_id: str) -> SemanticModel | None:
    """The newest approved structure version: what the compiler and governed answers use."""
    return session.scalar(select(SemanticModel).where(SemanticModel.workspace_id == workspace_id,
                                                      SemanticModel.status == "approved")
                          .order_by(SemanticModel.version.desc()).limit(1))


def _model_payload(row: SemanticModel) -> dict[str, Any]:
    return {"workspace_id": row.workspace_id, "version": row.version, "content_hash": row.content_hash}


def _accountable(session: Session, row: SemanticModel) -> str | None:
    """The person accountable for a version: the run's requester for an agent's, else its author."""
    if row.run_id:
        run = session.get(AnalysisRun, row.run_id)
        if run is not None and getattr(run, "requested_by", None):
            return run.requested_by
    return row.created_by.split(":", 1)[1] if row.created_by.startswith("user:") else None


def model_diff(session: Session, workspace_id: str, version: int | None = None) -> dict[str, Any]:
    """A structure version against the approved one (shown before approval)."""
    from analystos.semantic.diff import diff_semantic_object, model_snapshot

    rows = {r.version: r for r in session.scalars(select(SemanticModel).where(SemanticModel.workspace_id == workspace_id))}
    if not rows:
        raise NotFound("the workspace has no semantic model yet")
    row = rows.get(version) if version is not None else rows[max(rows)]
    if row is None:
        raise NotFound(f"semantic model version {version} not found")
    base = approved_model(session, workspace_id)
    base = base if base is not None and base.id != row.id else None
    diff = diff_semantic_object(model_snapshot(base.datasets, base.relationships) if base else None,
                                model_snapshot(row.datasets, row.relationships))
    return {"version": row.version, "status": row.status, "base_version": base.version if base else None, **diff.as_dict()}


def decide_model(session: Session, workspace_id: str, version: int, user: User, *, approve: bool,
                 reason: str | None = None) -> SemanticModel:
    """Approve or reject a proposed structure version. Hash-bound to the version's content; the approver
    may not be the person accountable for the proposal (separation of duties)."""
    from analystos.governance.approvals import decide, request_approval

    row = session.scalar(select(SemanticModel).where(SemanticModel.workspace_id == workspace_id, SemanticModel.version == version))
    if row is None:
        raise NotFound(f"semantic model version {version} not found")
    if row.status != "proposed":
        raise Conflict(f"semantic model version {version} is {row.status}, not proposed")
    newest = approved_model(session, workspace_id)
    if approve and newest is not None and newest.version > row.version:
        raise Conflict(f"version {version} is older than the approved version {newest.version}; review a newer proposal")
    requester = _accountable(session, row)
    if requester is None or requester == user.id:
        audit(f"user:{user.id}", "semantic.model.self_approval_blocked", workspace_id=workspace_id, target=row.id,
              decision="deny", reasons=["separation_of_duties"])
        raise Forbidden("separation of duties: the person accountable for a model proposal cannot decide it")
    ws = session.get(Workspace, workspace_id)
    approval = session.get(Approval, row.approval_id) if row.approval_id else None
    if approval is None or approval.status != "pending" or approval.policy_version != ws.policy_version:
        approval = request_approval(session, workspace_id=workspace_id, run_id=None, action=MODEL_APPROVAL_ACTION,
                                    payload=_model_payload(row), plan_hash=None, policy_version=ws.policy_version,
                                    requested_by=requester, risk_tier="medium", destination=None,
                                    affected_assets=[f"semantic_model:v{row.version}"],
                                    evidence={"diff": model_diff(session, workspace_id, row.version), "origin": row.origin,
                                              "run_id": row.run_id})
        row.approval_id = approval.id
    decide(session, approval.id, user, approve=approve, reason=reason)
    return apply_model_decision(session, approval)


def apply_model_decision(session: Session, approval: Approval) -> SemanticModel:
    from analystos.governance.approvals import verify_for_execution

    row = session.scalar(select(SemanticModel).where(SemanticModel.approval_id == approval.id))
    if row is None:
        raise NotFound(f"no semantic model version bound to approval {approval.id}")
    if row.status != "proposed" or approval.status not in ("approved", "rejected"):
        return row
    actor = f"user:{approval.decided_by}"
    if approval.status == "rejected":
        row.status, row.decided_by, row.decided_at = "deprecated", approval.decided_by, utcnow()
        audit(actor, "semantic.model.rejected", workspace_id=row.workspace_id, target=row.id, decision="deny",
              reasons=[approval.reason or ""], session=session)
        return row
    verify_for_execution(session, approval.id, payload=_model_payload(row), plan_hash=None)
    row.status, row.decided_by, row.decided_at = "approved", approval.decided_by, utcnow()
    approval.status = "executed"
    emit(row.workspace_id, "semantic.model.approved", {"version": row.version, "approval_id": approval.id},
         actor=actor, session=session)
    audit(actor, "semantic.model.approved", workspace_id=row.workspace_id, target=row.id, decision="allow",
          details={"version": row.version, "payload_hash": approval.payload_hash}, session=session)
    invalidate_stale(session, row.workspace_id, actor=actor)
    return row


# ------------------------------------------------------------------------------------ stale definitions
def invalidate_stale(session: Session, workspace_id: str, *, actor: str) -> list[dict[str, Any]]:
    """Deprecate approved metrics the newest approved structure no longer supports (P4-05). Metrics
    without a declared dataset are not checked (they are never compiled)."""
    from analystos.semantic.service import approved_metrics, definition

    model = approved_model(session, workspace_id)
    if model is None:
        return []
    out = []
    for name, row in approved_metrics(session, workspace_id).items():
        defn = definition(row)
        if not defn.dataset:
            continue
        problems = definition_problems(defn, model.datasets, model.relationships)
        if not problems:
            continue
        row.status, row.reason = "deprecated", f"stale after semantic model v{model.version}: {problems[0]}"
        row.decided_at = utcnow()
        out.append({"metric": name, "version": row.version, "problems": problems})
        emit(workspace_id, "semantic.metric.stale", {"metric": name, "version": row.version, "model_version": model.version,
                                                     "problems": problems}, actor=actor, session=session)
        audit(actor, "semantic.metric.stale", workspace_id=workspace_id, target=row.id, decision="deny",
              reasons=problems[:5], details={"model_version": model.version}, session=session)
    return out


# ------------------------------------------------------------------------------------ reconciliation
def reconcile(session: Session, workspace_id: str) -> dict[str, Any]:
    """Fan-out, denominator, stale and unvalidated-join findings over the live definitions."""
    from analystos.semantic.compiler import FANOUT, _additive, join_path
    from analystos.semantic.service import conflicts, current_model, definition, metric_rows
    model = current_model(session, workspace_id)  # the structure approvals check against (P4-05)
    model = approved_model(session, workspace_id) or current_model(session, workspace_id)
    datasets, relationships = (model.datasets, model.relationships) if model else ([], [])
    fanout, stale, invalid = [], [], []
    for row in metric_rows(session, workspace_id):
        if row.status not in ("proposed", "approved"):
            continue
        defn = definition(row)
        problems = definition_problems(defn, datasets, relationships) if defn.dataset else []
        if problems:
            (stale if row.status == "approved" else invalid).append({"metric": row.name, "version": row.version, "problems": problems})
        if not defn.dataset or ossie.metric_problem(defn):
            continue
        additive = _additive(sqlglot.parse_one(defn.expression, read=_read(defn.dialect)))
        for dim in defn.dimensions:
            ds_name = dim.rpartition(".")[0]
            if not ds_name or ds_name == defn.dataset:
                continue
            try:
                path = join_path(relationships, defn.dataset, ds_name)
            except InvalidInput as exc:
                fanout.append({"metric": row.name, "dimension": dim, "status": "blocked", "detail": exc.message})
                continue
            edges = [j for j in path if j.cardinality in FANOUT]
            status = "ok" if not edges or not additive else (
                "pre_aggregated" if all(j.relationship["name"] in defn.pre_aggregations for j in edges) else "fan_out")
            fanout.append({"metric": row.name, "dimension": dim, "status": status,
                           "edges": [{"relationship": j.relationship["name"], "cardinality": j.cardinality} for j in path]})
    unvalidated = [r["name"] for r in relationships if not SemanticRelationship.model_validate(r).validated]
    found = [c.model_dump() for c in conflicts(session, workspace_id)]
    pending = session.scalar(select(SemanticRelationshipCandidate.id).where(
        SemanticRelationshipCandidate.workspace_id == workspace_id, SemanticRelationshipCandidate.status == "pending").limit(1))
    return {"model_version": model.version if model else None, "fanout": fanout, "stale": stale, "invalid_proposals": invalid,
            "denominators": [c for c in found if c["kind"] == "denominator_mismatch"],
            "conflicts": [c for c in found if c["kind"] != "denominator_mismatch"],
            "unvalidated_relationships": unvalidated, "pending_relationship_candidates": pending is not None}


# ------------------------------------------------------------------------------------ relationship queue
def _scope_assets(session: Session, scope: Any, names: list[str] | None) -> dict[str, list[dict[str, Any]]]:
    """source_id -> the discovery shape of the selected assets in the caller's scope (denied columns out)."""
    wanted = {n.lower() for n in names} if names else None
    by_source: dict[str, list[dict[str, Any]]] = {}
    denied = {d.lower() for d in scope.denied_columns}
    for asset in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == scope.workspace_id,
                                                           SourceAsset.selected.is_(True))
                                 .order_by(SourceAsset.schema_name, SourceAsset.name)):
        fq = f"{asset.schema_name}.{asset.name}"
        if fq not in scope.assets or (wanted is not None and fq.lower() not in wanted):
            continue
        cols = session.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id).order_by(SourceColumn.ordinal))
        by_source.setdefault(asset.source_id, []).append({"asset": fq, "row_count": asset.row_count, "columns": [
            {"name": c.name, "data_type": c.data_type, "is_key": c.is_key, "nullable": getattr(c, "nullable", True),
             "references": (c.profile or {}).get("references")}
            for c in cols if f"{fq}.{c.name}".lower() not in denied]})
    if wanted is not None:
        missing = sorted(wanted - {a["asset"].lower() for group in by_source.values() for a in group})
        if missing:
            raise InvalidInput(f"not in your authorized scope: {', '.join(missing)}")
    return by_source


def _payload(row: SemanticRelationshipCandidate) -> dict[str, Any]:
    return {"workspace_id": row.workspace_id, "candidate_id": row.id, "from_asset": row.from_asset,
            "from_columns": row.from_columns, "to_asset": row.to_asset, "to_columns": row.to_columns,
            "cardinality": row.cardinality, "content_hash": row.content_hash}


def _content_hash(c: Any) -> str:
    ev = c.evidence
    return stable_hash({"from": c.from_asset, "from_columns": c.from_columns, "to": c.to_asset, "to_columns": c.to_columns,
                        "cardinality": c.cardinality, "counts": {k: ev.get(k) for k in (
                            "fk_rows", "fk_distinct", "matched_rows", "target_rows", "target_distinct")}})


def _request(session: Session, row: SemanticRelationshipCandidate) -> Approval:
    from analystos.governance.approvals import request_approval

    ws = session.get(Workspace, row.workspace_id)
    approval = request_approval(session, workspace_id=row.workspace_id, run_id=None, action=RELATIONSHIP_APPROVAL_ACTION,
                                payload=_payload(row), plan_hash=None, policy_version=ws.policy_version,
                                requested_by=row.proposed_by, risk_tier="medium", destination=None,
                                affected_assets=[f"table:{row.from_asset}", f"table:{row.to_asset}"],
                                evidence={"cardinality": row.cardinality, "containment": row.containment,
                                          "assessment": row.assessment, "measured_at": row.measured_at.isoformat()})
    row.approval_id = approval.id
    return approval


def record_candidate(session: Session, workspace_id: str, candidate: Any, *, source_id: str | None, origin: str,
                     proposed_by: str) -> SemanticRelationshipCandidate:
    """Store a measured candidate (skills.relationships.RelationshipCandidate) as pending, or refresh the
    pending one with the same columns (a changed measurement re-binds its approval)."""
    existing = session.scalar(select(SemanticRelationshipCandidate).where(
        SemanticRelationshipCandidate.workspace_id == workspace_id, SemanticRelationshipCandidate.status == "pending",
        SemanticRelationshipCandidate.from_asset == candidate.from_asset, SemanticRelationshipCandidate.to_asset == candidate.to_asset,
        SemanticRelationshipCandidate.from_columns == candidate.from_columns,
        SemanticRelationshipCandidate.to_columns == candidate.to_columns))
    digest = _content_hash(candidate)
    row = existing or SemanticRelationshipCandidate(
        id=new_id("relc"), workspace_id=workspace_id, from_asset=candidate.from_asset, from_columns=list(candidate.from_columns),
        to_asset=candidate.to_asset, to_columns=list(candidate.to_columns), origin=origin, proposed_by=proposed_by,
        status="pending")
    changed = existing is None or existing.content_hash != digest
    row.source_id, row.cardinality, row.confidence = source_id, candidate.cardinality, candidate.confidence
    row.containment = float(candidate.evidence.get("containment") or 0.0)
    row.evidence, row.assessment, row.measured_at, row.content_hash = candidate.evidence, candidate.assessment, utcnow(), digest
    session.add(row)
    session.flush()
    if changed:
        old = session.get(Approval, row.approval_id) if row.approval_id else None
        if old is not None and old.status in ("pending", "approved"):
            old.status, old.reason = "invalidated", "re-measured"
        _request(session, row)
        emit(workspace_id, "semantic.relationship.candidate", {"candidate_id": row.id, "from": row.from_asset,
                                                               "to": row.to_asset, "cardinality": row.cardinality},
             actor=f"user:{proposed_by}" if origin == "user" or origin == "discovered" else origin, session=session)
    return row


def _runner(session: Session, user: User, workspace_id: str, source_id: str) -> Any:
    from analystos.governance.policy import resolve_scope
    from analystos.runtime.context import default_gateway

    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="editor")
    return default_gateway().run_sql_for(scope, actor=f"user:{user.id}", source_id=source_id), scope


def discover_candidates(session: Session, user: User, workspace_id: str, *, assets: list[str] | None = None,
                        composite: bool = True) -> list[SemanticRelationshipCandidate]:
    """Measure relationship candidates between the caller's selected assets, one source at a time, through
    the gateway as the caller, and queue them for review."""
    from analystos.governance.policy import resolve_scope
    from analystos.skills.relationships import discover_composite_relationships, discover_relationships

    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="editor")
    out = []
    for source_id, group in sorted(_scope_assets(session, scope, assets).items()):
        run_sql, _ = _runner(session, user, workspace_id, source_id)
        found = discover_relationships(run_sql, group)
        if composite:
            found += discover_composite_relationships(run_sql, group)
        for c in found:
            out.append(record_candidate(session, workspace_id, c, source_id=source_id, origin="discovered",
                                        proposed_by=user.id))
    audit(f"user:{user.id}", "semantic.relationship.discovered", workspace_id=workspace_id,
          details={"candidates": len(out), "assets": assets}, session=session)
    return out


def propose_candidate(session: Session, user: User, workspace_id: str, *, from_asset: str, from_columns: list[str],
                      to_asset: str, to_columns: list[str], origin: str = "user") -> SemanticRelationshipCandidate:
    """A person's (or an agent's) join proposal: columns only. It is measured before it is queued."""
    from analystos.governance.policy import resolve_scope
    from analystos.skills.relationships import measure_columns, with_assessment

    if not from_columns or len(from_columns) != len(to_columns) or len(from_columns) > 3:
        raise InvalidInput("give 1 to 3 from_columns and the same number of to_columns")
    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="editor")
    groups = _scope_assets(session, scope, [from_asset, to_asset])
    sources = [s for s, group in groups.items() if {a["asset"].lower() for a in group} >= {from_asset.lower(), to_asset.lower()}]
    if len(sources) != 1:
        raise InvalidInput("both assets must be in one source of your scope (cross-source joins are not relationships)")
    group = groups[sources[0]]
    names = {a["asset"].lower(): a for a in group}
    for asset, cols in ((from_asset, from_columns), (to_asset, to_columns)):
        known = {c["name"].lower() for c in names[asset.lower()]["columns"]}
        unknown = [c for c in cols if c.lower() not in known]
        if unknown:
            raise InvalidInput(f"{asset} has no readable column {', '.join(unknown)}")
    run_sql, _ = _runner(session, user, workspace_id, sources[0])
    measured = measure_columns(run_sql, run_sql.dialect, names[from_asset.lower()]["asset"], from_columns,
                               names[to_asset.lower()]["asset"], to_columns, "declared" if origin == "user" else origin)
    if measured is None:
        raise InvalidInput("the from-columns hold no non-null values: nothing to measure")
    return record_candidate(session, workspace_id, with_assessment(measured, group), source_id=sources[0], origin=origin,
                            proposed_by=user.id)


def candidates(session: Session, workspace_id: str, status: str | None = None) -> list[SemanticRelationshipCandidate]:
    stmt = select(SemanticRelationshipCandidate).where(SemanticRelationshipCandidate.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(SemanticRelationshipCandidate.status == status)
    return list(session.scalars(stmt.order_by(SemanticRelationshipCandidate.created_at, SemanticRelationshipCandidate.id)))


def decide_candidate(session: Session, workspace_id: str, candidate_id: str, user: User, *, approve: bool,
                     reason: str | None = None) -> SemanticRelationshipCandidate:
    from analystos.governance.approvals import decide

    row = session.get(SemanticRelationshipCandidate, candidate_id)
    if row is None or row.workspace_id != workspace_id:
        raise NotFound(f"relationship candidate {candidate_id} not found")
    if row.status != "pending":
        raise Conflict(f"relationship candidate is {row.status}")
    if row.proposed_by == user.id:
        audit(f"user:{user.id}", "semantic.relationship.self_approval_blocked", workspace_id=workspace_id, target=row.id,
              decision="deny", reasons=["separation_of_duties"])
        raise Forbidden("separation of duties: the person who proposed or measured a relationship cannot decide it")
    if approve:
        _check_acceptable(row)
    approval = session.get(Approval, row.approval_id) if row.approval_id else None
    ws = session.get(Workspace, workspace_id)
    if approval is None or approval.status != "pending" or approval.expires_at < utcnow() \
            or approval.policy_version != ws.policy_version:
        approval = _request(session, row)
    decide(session, approval.id, user, approve=approve, reason=reason)
    return apply_candidate_decision(session, approval)


def _check_acceptable(row: SemanticRelationshipCandidate) -> None:
    measured_at = row.measured_at if row.measured_at.tzinfo else row.measured_at.replace(tzinfo=utcnow().tzinfo)
    if utcnow() - measured_at > MEASUREMENT_MAX_AGE:
        raise Conflict("the measurement is older than 7 days; re-measure the candidate before deciding")
    if not (row.assessment or {}).get("approvable"):
        warnings = ", ".join((row.assessment or {}).get("warnings") or []) or "no corroborating evidence"
        raise Conflict("this join rests only on matching names and types, which is not evidence that one table references "
                       f"the other ({warnings}). It needs a declared foreign key, a key on exactly one side for a name more "
                       "specific than a bare identifier, or joins observed in query history.",
                       details={"assessment": row.assessment})


def _dataset_for(session: Session, datasets: list[dict[str, Any]], asset: str, workspace_id: str) -> tuple[str, SemanticDataset | None]:
    """The model dataset that reads exactly `asset`, or a new one named after the table."""
    for d in datasets:
        src = (d.get("source") or "").strip()
        if src.lower() == asset.lower():
            return d["name"], None
        try:
            tree = sqlglot.parse_one(src)
        except sqlglot.errors.SqlglotError:
            continue
        tables = list(tree.find_all(exp.Table))
        if isinstance(tree, exp.Select) and len(tables) == 1 and not any(tree.find_all(exp.Join)) \
                and f"{tables[0].db}.{tables[0].name}".lower() == asset.lower():
            return d["name"], None
    taken = {d["name"] for d in datasets}
    base = asset.split(".")[-1]
    name = base if base not in taken else asset.replace(".", "_")
    schema_name, table_name = asset.split(".", 1)
    columns = session.scalars(select(SourceColumn.name).join(SourceAsset, SourceAsset.id == SourceColumn.asset_id)
                              .where(SourceAsset.workspace_id == workspace_id, SourceAsset.schema_name == schema_name,
                                     SourceAsset.name == table_name).order_by(SourceColumn.ordinal))
    fields = [SemanticField(name=c, expressions=[DialectExpression(expression=c)]) for c in columns]
    return name, SemanticDataset(name=name, source=asset, fields=fields)


def apply_candidate_decision(session: Session, approval: Approval) -> SemanticRelationshipCandidate:
    from analystos.artifacts.registry import link
    from analystos.governance.approvals import verify_for_execution
    from analystos.semantic.service import current_model, save_model

    row = session.scalar(select(SemanticRelationshipCandidate).where(SemanticRelationshipCandidate.approval_id == approval.id))
    if row is None:
        raise NotFound(f"no relationship candidate bound to approval {approval.id}")
    if row.status != "pending" or approval.status not in ("approved", "rejected"):
        return row
    actor = f"user:{approval.decided_by}"
    row.decided_by, row.decided_at, row.reason = approval.decided_by, utcnow(), approval.reason
    if approval.status == "rejected":
        row.status = "rejected"
        audit(actor, "semantic.relationship.rejected", workspace_id=row.workspace_id, target=row.id, decision="deny",
              reasons=[approval.reason or ""], session=session)
        return row
    _check_acceptable(row)
    verify_for_execution(session, approval.id, payload=_payload(row), plan_hash=None)
    model = current_model(session, row.workspace_id)
    datasets = list(model.datasets) if model else []
    from_ds, new_from = _dataset_for(session, datasets, row.from_asset, row.workspace_id)
    if new_from is not None:
        datasets.append(new_from.model_dump(mode="json"))
    to_ds, new_to = _dataset_for(session, datasets, row.to_asset, row.workspace_id)
    name = f"{from_ds}__{to_ds}__{'_'.join(row.from_columns)}"[:200]
    relationship = SemanticRelationship.model_validate({
        "name": name, "from": from_ds, "to": to_ds, "from_columns": row.from_columns, "to_columns": row.to_columns,
        "cardinality": row.cardinality, "validated_at": utcnow().isoformat(), "validated_by": approval.decided_by,
        "ai_context": {"instructions": f"Measured through the gateway: containment {row.containment:.4f}; accepted in review."}})
    save_model(session, row.workspace_id, actor=actor, origin="relationship_review",
               datasets=[d for d in (new_from, new_to) if d is not None], relationships=[relationship])
    row.status, row.relationship_name = "accepted", name
    approval.status = "executed"
    link(session, row.workspace_id, ("table", row.from_asset), "joins_to", ("table", row.to_asset))
    link(session, row.workspace_id, ("relationship_candidate", row.id), "defines", ("semantic_relationship", name))
    emit(row.workspace_id, "semantic.relationship.validated", {"relationship": name, "cardinality": row.cardinality,
                                                               "candidate_id": row.id}, actor=actor, session=session)
    audit(actor, "semantic.relationship.accepted", workspace_id=row.workspace_id, target=row.id, decision="allow",
          details={"relationship": name, "cardinality": row.cardinality, "payload_hash": approval.payload_hash,
                   "proposed_by": row.proposed_by}, session=session)
    return row
