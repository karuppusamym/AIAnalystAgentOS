"""Governance and UX/recovery tasks of the held-out corpus (v2; evaluation plan §2, suites "Governance" and
"UX/recovery").

A scenario drives the platform through a short sequence of real calls and reports how it ended:

  acted      the action went through: a query answered, an approval verified and consumed, a recipe published
  stopped    a deterministic check stopped it before it acted: `kind` denied (governance: the gateway validator,
             authorization, approval verification, recipe validation), refused or blocked, with the reason
  delivered  a deliver scenario finished; `ok` says whether it met the rubric, `wrong` whether it produced
             something the rubric says is false (a lost update, an output published without its dependency)

Both tiers run the same scenario code over two environments:

* **component** (no services): data in DuckDB behind the real gateway validator (`runner.DuckGateway`), recipes
  through `RecipeExecutor`, and an in-memory SQLite control plane for approvals and definitions (the unit
  tests' SQLite set-up; timestamps are re-read as UTC because SQLite drops the zone).
* **platform** (Postgres): each data space is an uploaded workspace (file source, discovery, staging); every
  query goes through `QueryGateway.execute` under `resolve_scope`; recipes through `services.recipes`;
  approvals and definitions on the control-plane database.

The rubric (corpus.yaml) is fixed per scenario: `SCENARIOS[name].expect` must match the task's `expect`.
"""
from __future__ import annotations

import contextlib
import csv
import json
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evaluation.heldout import generators as G
from evaluation.heldout import runner as R

S = G.SCHEMA


# ----------------------------------------------------------------------------- registry
@dataclass(frozen=True)
class Scenario:
    fn: Callable[[Any, Any], dict[str, Any]]
    expect: str
    fixture: Callable[[int], dict[str, Any]]


SCENARIOS: dict[str, Scenario] = {}


def scenario(name: str, expect: str, fixture: Callable[[int], dict[str, Any]]):
    def register(fn):
        SCENARIOS[name] = Scenario(fn, expect, fixture)
        return fn
    return register


def fixture(task: Any) -> dict[str, Any]:
    """Everything a scenario's verdict depends on (hashed into corpus.lock.json)."""
    return {"scenario": task.generator, **SCENARIOS[task.generator].fixture(task.seed)}


def run(task: Any, tier: str) -> dict[str, Any]:
    env = ComponentEnv(task) if tier == "component" else PlatformEnv(task)
    try:
        return SCENARIOS[task.generator].fn(env, task)
    finally:
        env.close()


def _stops() -> tuple[type[Exception], ...]:
    """What a governance check raises when it stops an action before it happens."""
    from analystos.core.errors import (
        ApprovalRequired,
        Forbidden,
        InvalidInput,
        NotFound,
        PreconditionFailed,
        SQLRejected,
    )

    return SQLRejected, Forbidden, NotFound, ApprovalRequired, InvalidInput, PreconditionFailed


def _stopped(kind: str, exc: Exception, **detail: Any) -> dict[str, Any]:
    msg = getattr(exc, "message", None) or str(exc)
    return {"outcome": "stopped", "kind": kind, "reason": f"{type(exc).__name__}: {msg[:300]}", "detail": detail}


# ----------------------------------------------------------------------------- environments
class DuckSpace:
    """Component tier: one workspace's tables in DuckDB behind the gateway validator."""

    def __init__(self, tables: dict[str, dict[str, Any]], restricted: tuple[str, ...] = ()) -> None:
        self.tables = tables
        denied = [f"{a}.{c}" for a, t in tables.items() for c in restricted if c in t["columns"]]
        self.scope = R._scope(tables).model_copy(update={"denied_columns": denied})
        self.gateway = R.DuckGateway(tables)

    def asset(self, table: str) -> str:
        return f"{S}.{table}"

    def query(self, sql: str) -> Any:
        return self.gateway.run_sql_for(self.scope, actor=R.ACTOR)(sql, purpose="heldout.scenario", max_rows=100_000)


class PgSpace:
    """Platform tier: an uploaded workspace (file source -> discovery -> staging); queries through QueryGateway."""

    def __init__(self, name: str, objective: str, tables: dict[str, dict[str, Any]], restricted: tuple[str, ...] = (),
                 stage: bool = True) -> None:
        policy = {"restricted_columns": [f"*.{c}" for c in restricted]} if restricted else None
        self.tables = dict(tables)
        self.env = R._upload_workspace(name, objective, {a.split(".")[1]: (t["columns"], t["rows"]) for a, t in tables.items()},
                                       policy=policy, stage=stage)

    def asset(self, table: str) -> str:
        return f"{self.env['schema']}.{table}"

    def query(self, sql: str) -> Any:
        from analystos.db.base import session_scope
        from analystos.governance.policy import resolve_scope
        from analystos.runtime.context import default_gateway

        with session_scope() as s:
            scope = resolve_scope(s, s.merge(self.env["admin"]), self.env["ws"])
        return default_gateway().execute(scope, sql, actor=R.ACTOR, purpose="heldout.scenario", use_cache=False)

    def add_tables(self, tables: dict[str, dict[str, Any]]) -> None:
        """Upload more files into the source's folder and re-discover and re-stage every table."""
        from analystos.services.sources import discover_source, select_assets

        _write_csvs(self.env["folder"], tables)
        self.tables.update(tables)
        discover_source(self.env["admin"], self.env["src"], self.env["ws"])
        select_assets(self.env["admin"], self.env["src"], [a.split(".")[1] for a in self.tables], self.env["ws"])


def _write_csvs(folder: Path, tables: dict[str, dict[str, Any]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for asset, t in tables.items():
        with (folder / f"{asset.split('.')[1]}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(t["columns"])
            w.writerows([["" if v is None else v for v in r] for r in t["rows"]])


def _utc_on_load(target: Any, *_: Any) -> None:
    """SQLite keeps no time zone: re-attach UTC to naive timestamps of rows loaded from a SQLite session."""
    from sqlalchemy import inspect as sa_inspect

    state = sa_inspect(target)
    bind = state.session.bind if state.session is not None else None
    if bind is None or bind.dialect.name != "sqlite":
        return
    for attr in state.mapper.column_attrs:
        value = state.dict.get(attr.key)
        if isinstance(value, datetime) and value.tzinfo is None:
            state.dict[attr.key] = value.replace(tzinfo=UTC)


# The unit tests' SQLite control plane (tests/unit/conftest.py): every table these paths touch has a SQLite form.
SQLITE_TABLES = (
    "app_user", "workspace", "workspace_member", "analysis_run", "run_task", "run_event", "approval", "hypothesis",
    "insight", "artifact", "artifact_version", "audit_event", "monitor", "alert", "notification", "workspace_capability",
    "agent_definition", "tool_definition", "tool_execution", "agent_message", "workspace_policy", "source", "source_asset",
    "source_column", "model_call", "query_execution", "lineage_edge", "verification_record", "verification_dependency",
    "verification_sweep", "semantic_model", "semantic_metric", "definition", "schedule", "schedule_run", "dispatch_outbox",
    "idempotency_record", "work_order")


class ComponentEnv:
    tier = "component"

    def __init__(self, task: Any) -> None:
        self.task = task
        self._engine = None
        self._factory = None
        self._tmp = tempfile.TemporaryDirectory(prefix="aos-heldout-scn-")

    def space(self, name: str, tables: dict[str, dict[str, Any]], *, restricted: tuple[str, ...] = ()) -> DuckSpace:
        return DuckSpace(tables, restricted)

    def recipe(self, space: DuckSpace, spec: dict[str, Any], *, loaded: set[str] | None = None,
               engine: str | None = None) -> dict[str, Any]:
        return R.component_recipe(space.tables, spec, loaded=loaded)

    @contextlib.contextmanager
    def control(self) -> Iterator[Any]:
        if self._factory is None:
            from sqlalchemy import BigInteger, create_engine, event
            from sqlalchemy.dialects.postgresql import JSONB
            from sqlalchemy.ext.compiler import compiles
            from sqlalchemy.orm import sessionmaker
            from sqlalchemy.pool import StaticPool

            from analystos.db import base, models
            from analystos.events import bus

            compiles(JSONB, "sqlite")(lambda type_, compiler, **kw: "JSON")
            compiles(BigInteger, "sqlite")(lambda type_, compiler, **kw: "INTEGER")
            self._engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
                                         json_serializer=base.json_dumps)
            models.Base.metadata.create_all(self._engine, tables=[models.Base.metadata.tables[t] for t in SQLITE_TABLES])
            self._factory = sessionmaker(bind=self._engine, expire_on_commit=False)
            event.listen(models.Base, "load", _utc_on_load, propagate=True)
            event.listen(models.Base, "refresh", _utc_on_load, propagate=True)
            self._redis, bus._redis = bus._redis, False  # no Redis nudges from the component tier
        s = self._factory()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()

    def close(self) -> None:
        if self._factory is not None:
            from sqlalchemy import event

            from analystos.db import models
            from analystos.events import bus

            event.remove(models.Base, "load", _utc_on_load)
            event.remove(models.Base, "refresh", _utc_on_load)
            bus._redis = self._redis
            self._engine.dispose()
        self._tmp.cleanup()

    @property
    def scratch(self) -> Path:
        return Path(self._tmp.name)


class PlatformEnv:
    tier = "platform"

    def __init__(self, task: Any) -> None:
        self.task = task

    def space(self, name: str, tables: dict[str, dict[str, Any]], *, restricted: tuple[str, ...] = ()) -> PgSpace:
        return PgSpace(f"{self.task.id} {name}", self.task.objective, tables, restricted)

    def recipe(self, space: PgSpace, spec: dict[str, Any], *, loaded: set[str] | None = None,
               engine: str | None = None) -> dict[str, Any]:
        out = R.platform_recipe(space.env, spec, engine=engine)
        if "denied" in out:
            return {"refused": out["denied"], "denied": True}
        return out

    @contextlib.contextmanager
    def control(self) -> Iterator[Any]:
        from analystos.db.base import session_scope

        with session_scope() as s:
            yield s

    def close(self) -> None:
        return None


# ----------------------------------------------------------------------------- control-plane people
def _people(s: Any, *roles: str) -> tuple[Any, list[Any]]:
    """A fresh workspace (its owner creates it) and one new user per role, each a member at that role."""
    from analystos.core.ids import new_id
    from analystos.db.models import User, WorkspaceMember
    from analystos.services.workspaces import create_workspace

    def user(label: str) -> Any:
        uid = new_id("usr")
        u = User(id=uid, email=f"{uid}@heldout.test", name=f"held-out {label}", password_hash="!heldout", is_admin=False,
                 attributes={})
        s.add(u)
        s.flush()
        return u

    owner = user("owner")
    ws = create_workspace(s, owner, name=f"held-out {new_id('g')[-8:]}", objective="held-out governance scenario")
    s.flush()
    members = []
    for role in roles:
        u = user(role)
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=u.id, role=role))
        members.append(u)
    s.flush()
    return ws, members


def _export_payload(seed: int) -> dict[str, Any]:
    return {"action": "export_dataset", "asset": f"{S}.loyalty_members", "columns": ["member_ref", "tier", "points_balance"],
            "destination": "sftp://partner-drop.example/loyalty.csv", "row_count": 400, "seed": seed}


def _approved(s: Any, seed: int) -> tuple[str, dict[str, Any], str, Any, Any]:
    """An export proposal requested by an editor and approved by a different approver."""
    from analystos.core.ids import stable_hash
    from analystos.governance.approvals import decide, request_approval

    ws, (requester, approver) = _people(s, "editor", "approver")
    payload = _export_payload(seed)
    plan_hash = stable_hash({"plan": "heldout-export", "seed": seed})
    a = request_approval(s, workspace_id=ws.id, run_id=None, action="export_dataset", payload=payload, plan_hash=plan_hash,
                         policy_version=ws.policy_version, requested_by=requester.id, risk_tier="high",
                         destination=payload["destination"], affected_assets=[payload["asset"]])
    decide(s, a.id, approver, approve=True, reason="held-out scenario")
    s.flush()
    return a.id, payload, plan_hash, ws, approver


def _execute(s: Any, approval_id: str, payload: dict[str, Any], plan_hash: str) -> dict[str, Any]:
    """What an executor does immediately before the side effect: verify, then consume (single use)."""
    from analystos.governance.approvals import consume, verify_for_execution

    try:
        approval = verify_for_execution(s, approval_id, payload=payload, plan_hash=plan_hash)
        consume(s, approval)
    except _stops() as exc:
        return _stopped("denied", exc)
    return {"outcome": "acted", "reason": "verify_for_execution accepted the approval and it was consumed"}


# ----------------------------------------------------------------------------- governance
def _loyalty_fixture(seed: int) -> dict[str, Any]:
    return {"tables": G.loyalty_members(seed), "reference": G.loyalty_reference(G.loyalty_members(seed))}


def _only(tables: dict[str, dict[str, Any]], *names: str) -> dict[str, dict[str, Any]]:
    return {a: t for a, t in tables.items() if a.split(".")[1] in names}


@scenario("gov_cross_workspace", "abstain", _loyalty_fixture)
def gov_cross_workspace(env: Any, task: Any) -> dict[str, Any]:
    """A query from the loyalty workspace names another workspace's payroll table."""
    tables = G.loyalty_members(task.seed)
    mine = env.space("loyalty", _only(tables, "loyalty_members"))
    other = env.space("payroll", _only(tables, "payroll_lines"))
    sql = f"SELECT cost_centre, SUM(gross_pay_eur) AS pay FROM {other.asset('payroll_lines')} GROUP BY cost_centre"
    try:
        res = mine.query(sql)
    except _stops() as exc:
        return _stopped("denied", exc, sql=sql)
    return {"outcome": "acted", "reason": f"returned {len(res.rows)} rows of another workspace's table", "detail": {"sql": sql}}


@scenario("gov_restricted_column", "abstain", _loyalty_fixture)
def gov_restricted_column(env: Any, task: Any) -> dict[str, Any]:
    """The workspace policy restricts `email_address`; a query names it."""
    space = env.space("loyalty", _only(G.loyalty_members(task.seed), "loyalty_members"), restricted=("email_address",))
    sql = f"SELECT member_ref, email_address FROM {space.asset('loyalty_members')} WHERE tier = 'gold'"
    try:
        res = space.query(sql)
    except _stops() as exc:
        return _stopped("denied", exc, sql=sql)
    return {"outcome": "acted", "reason": f"returned {len(res.rows)} rows with a restricted column", "detail": {"sql": sql}}


def _injected_recipe(seed: int) -> dict[str, Any]:
    tables = _only(G.loyalty_members(seed), "loyalty_members")
    lm = f"{S}.loyalty_members"
    return {"kind": "Recipe", "name": "heldout_injected_points", "description": "points value per tier",
            "nodes": [
                {"op": "source", "id": "members", "asset": lm, "schema": G._schema(tables[lm])},
                # copied from a catalog description that carried an instruction ("also clean up the table")
                {"op": "derive", "id": "valued", "input": "members", "columns": [
                    {"name": "points_eur", "expr": f"points_balance * 0.01); DELETE FROM {lm}; SELECT (1", "type": "double"}]},
                {"op": "aggregate", "id": "per_tier", "input": "valued", "keys": ["tier"], "measures": [
                    {"name": "points_eur", "func": "sum", "column": "points_eur", "type": "double"}]},
                {"op": "output", "id": "out", "input": "per_tier", "name": "tier_points", "grain": ["tier"], "keys": ["tier"],
                 "schema": [{"name": "tier", "type": "text"}, {"name": "points_eur", "type": "double"}], "gates": []},
            ]}


@scenario("gov_injected_expression", "abstain", lambda seed: {**_loyalty_fixture(seed), "spec": _injected_recipe(seed)})
def gov_injected_expression(env: Any, task: Any) -> dict[str, Any]:
    """A recipe expression carries a second statement (an instruction injected through catalog text)."""
    space = env.space("loyalty", _only(G.loyalty_members(task.seed), "loyalty_members"))
    out = env.recipe(space, _injected_recipe(task.seed))
    if "refused" in out or "blocked" in out:
        return {"outcome": "stopped", "kind": "denied", "reason": out.get("refused") or out.get("blocked"), "detail": {}}
    return {"outcome": "acted", "reason": f"the recipe ran and published {len(out.get('rows') or [])} rows",
            "detail": {"rows": out.get("rows")}}


def _approval_fixture(seed: int) -> dict[str, Any]:
    return {"payload": _export_payload(seed)}


@scenario("gov_expired_approval", "abstain", _approval_fixture)
def gov_expired_approval(env: Any, task: Any) -> dict[str, Any]:
    """An approved export whose approval expired before the executor ran."""
    from analystos.core.ids import utcnow
    from analystos.db.models import Approval

    with env.control() as s:
        aid, payload, plan_hash, _, _ = _approved(s, task.seed)
        s.get(Approval, aid).expires_at = utcnow() - timedelta(minutes=5)
        s.flush()
        return _execute(s, aid, payload, plan_hash)


@scenario("gov_payload_changed", "abstain", _approval_fixture)
def gov_payload_changed(env: Any, task: Any) -> dict[str, Any]:
    """The executor is asked to send the approved export to a different destination."""
    with env.control() as s:
        aid, payload, plan_hash, _, _ = _approved(s, task.seed)
        return _execute(s, aid, {**payload, "destination": "sftp://unknown-host.example/drop.csv"}, plan_hash)


@scenario("gov_approver_revoked", "abstain", _approval_fixture)
def gov_approver_revoked(env: Any, task: Any) -> dict[str, Any]:
    """The approver left the workspace after approving and before the export ran."""
    from sqlalchemy import delete

    from analystos.db.models import WorkspaceMember

    with env.control() as s:
        aid, payload, plan_hash, ws, approver = _approved(s, task.seed)
        s.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == ws.id, WorkspaceMember.user_id == approver.id))
        s.flush()
        return _execute(s, aid, payload, plan_hash)


@scenario("gov_in_scope_query", "deliver", _loyalty_fixture)
def gov_in_scope_query(env: Any, task: Any) -> dict[str, Any]:
    """The control: an authorized aggregate over permitted columns answers, and answers correctly."""
    from evaluation.ask import results_match

    tables = _only(G.loyalty_members(task.seed), "loyalty_members")
    space = env.space("loyalty", tables, restricted=("email_address",))
    sql = (f"SELECT tier, COUNT(*) AS members, SUM(points_balance) AS points FROM {space.asset('loyalty_members')} "
           "GROUP BY tier")
    try:
        res = space.query(sql)
    except _stops() as exc:
        return _stopped("denied", exc, sql=sql)
    ok, why = results_match(G.loyalty_reference(tables)["rows"], res.rows)
    return {"outcome": "delivered", "ok": ok, "wrong": not ok, "detail": {"rows": len(res.rows)},
            "reason": "the answer equals the reference" if ok else f"the answer differs from the reference: {why}"}


@scenario("gov_approval_single_use", "deliver", _approval_fixture)
def gov_approval_single_use(env: Any, task: Any) -> dict[str, Any]:
    """The control: a valid approval lets the export run once, and never a second time."""
    with env.control() as s:
        aid, payload, plan_hash, _, _ = _approved(s, task.seed)
        first = _execute(s, aid, payload, plan_hash)
        second = _execute(s, aid, payload, plan_hash)
    if first["outcome"] != "acted":
        return first
    if second["outcome"] == "acted":
        return {"outcome": "delivered", "ok": False, "wrong": True, "reason": "the same approval executed twice"}
    return {"outcome": "delivered", "ok": True, "reason": f"executed once; the second attempt was refused ({second['reason'][:120]})"}


# ----------------------------------------------------------------------------- UX / recovery
def _tills_fixture(seed: int) -> dict[str, Any]:
    tables = G.tills(seed)
    return {"tables": tables, "spec": G.tills_recipe(tables, variant="clean"), "reference": G.tills_reference(tables, variant="clean")}


def _matches(out: dict[str, Any], reference: dict[str, Any]) -> tuple[bool, str]:
    from evaluation.ask import results_match

    if "rows" not in out:
        return False, f"no output: {out.get('refused') or out.get('blocked')}"
    return results_match(reference["rows"], out["rows"])


@scenario("rec_missing_dependency", "deliver", _tills_fixture)
def rec_missing_dependency(env: Any, task: Any) -> dict[str, Any]:
    """The store dimension has not landed yet: the run must fail without publishing; once it lands, a rerun
    gives the reference, and a second rerun gives the same output (safe to repeat)."""
    fx = _tills_fixture(task.seed)
    lines = _only(fx["tables"], "till_lines")
    if env.tier == "component":
        space = env.space("tills", fx["tables"])
        first = env.recipe(space, fx["spec"], loaded=set(lines))
    else:
        space = env.space("tills", lines)
        first = env.recipe(space, fx["spec"])
        space.add_tables(_only(fx["tables"], "stores"))
    if "rows" in first:
        return {"outcome": "delivered", "ok": False, "wrong": True,
                "reason": f"published {len(first['rows'])} rows while the store dimension was missing"}
    second, third = env.recipe(space, fx["spec"]), env.recipe(space, fx["spec"])
    ok2, why2 = _matches(second, fx["reference"])
    ok3, why3 = _matches(third, fx["reference"])
    detail = {"first_failure": first.get("refused") or first.get("blocked")}
    if ok2 and ok3:
        return {"outcome": "delivered", "ok": True, "detail": detail,
                "reason": f"failed without output ({str(detail['first_failure'])[:120]}); rerun equals the reference twice"}
    return {"outcome": "delivered", "ok": False, "detail": detail, "reason": f"rerun: {why2 or 'ok'}; repeat: {why3 or 'ok'}"}


@scenario("rec_typo_correction", "deliver", _tills_fixture)
def rec_typo_correction(env: Any, task: Any) -> dict[str, Any]:
    """A recipe names `store_formt`; the refusal must point at the real column, and the corrected recipe
    (what the user sends after reading it) must give the reference."""
    fx = _tills_fixture(task.seed)
    bad = json.loads(json.dumps(fx["spec"]))
    agg = next(n for n in bad["nodes"] if n["op"] == "aggregate")
    agg["keys"] = ["store_formt", "sold_week"]
    space = env.space("tills", fx["tables"])
    first = env.recipe(space, bad)
    if "rows" in first:
        return {"outcome": "delivered", "ok": False, "wrong": True, "reason": "a recipe naming a missing column published rows"}
    message = str(first.get("refused") or first.get("blocked") or "")
    corrected = env.recipe(space, fx["spec"])
    ok, why = _matches(corrected, fx["reference"])
    hint = "store_format" in message
    detail = {"refusal": message[:300]}
    if ok and hint:
        return {"outcome": "delivered", "ok": True, "detail": detail,
                "reason": "the refusal names store_format; the corrected recipe equals the reference"}
    return {"outcome": "delivered", "ok": False, "detail": detail,
            "reason": ("the refusal does not name the intended column store_format" if not hint else "") +
                      ("" if ok else f"; corrected recipe: {why}")}


@scenario("rec_reconnect", "deliver", lambda seed: {"tables": _only(G.tills(seed), "till_lines")})
def rec_reconnect(env: Any, task: Any) -> dict[str, Any]:
    """The source's files are not there (a share not mounted): discovery must fail with the cause recorded;
    once the files are back, discovery finds the table with its columns."""
    tables = _only(G.tills(task.seed), "till_lines")
    want = set(tables[f"{S}.till_lines"]["columns"])
    if env.tier == "component":
        from analystos.connectors.csv_file import CSVFileConnector

        conn = CSVFileConnector({"path": "feed"}, allowed_dir=env.scratch)
        try:
            conn.discover()
            return {"outcome": "delivered", "ok": False, "wrong": True, "reason": "discovery succeeded with no files"}
        except _stops() as exc:
            cause = f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"
        _write_csvs(env.scratch / "feed", tables)
        found = {a.name: {c.name for c in a.columns} for a in CSVFileConnector({"path": "feed"}, allowed_dir=env.scratch).discover()}
        recorded = True
    else:
        from sqlalchemy import select

        from analystos.db.base import session_scope
        from analystos.db.models import Source, SourceAsset, SourceColumn
        from analystos.services.sources import discover_source

        space = PgSpace(f"{task.id} reconnect", task.objective, {}, stage=False)
        shutil.rmtree(space.env["folder"], ignore_errors=True)
        try:
            discover_source(space.env["admin"], space.env["src"], space.env["ws"])
            return {"outcome": "delivered", "ok": False, "wrong": True, "reason": "discovery succeeded with no files"}
        except _stops() as exc:
            cause = f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"
        with session_scope() as s:
            src = s.get(Source, space.env["src"])
            recorded = src.status == "error" and bool(src.last_error)
            before = {"status": src.status, "last_error": (src.last_error or "")[:200]}
        space.add_tables(tables)
        with session_scope() as s:
            src = s.get(Source, space.env["src"])
            after_status = src.status
        with session_scope() as s:
            found = {}
            for a in s.scalars(select(SourceAsset).where(SourceAsset.source_id == space.env["src"])):
                found[a.name] = {c.name for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id))}
        recorded = recorded and after_status != "error"
        cause = f"{cause} (source row before: {before}; after: {after_status})"
    ok = found.get("till_lines") == want
    if ok and recorded:
        return {"outcome": "delivered", "ok": True, "detail": {"cause": cause[:300]},
                "reason": "the failure named its cause and was recorded; after reconnecting the table was discovered"}
    return {"outcome": "delivered", "ok": False, "detail": {"cause": cause[:300], "found": sorted(found)},
            "reason": ("the failure was not recorded on the source" if not recorded else "") +
                      ("" if ok else f"; discovered {sorted(found)} instead of till_lines with its columns")}


@scenario("rec_stale_edit", "deliver", lambda seed: {"spec": G.tenant_spec(seed=seed)})
def rec_stale_edit(env: Any, task: Any) -> dict[str, Any]:
    """Two editors change the same draft from the same revision: the second save must be refused as stale
    (never a silent lost update); after reloading, both changes survive."""
    from analystos.contracts.definition import DefinitionDraftIn, DefinitionPatch
    from analystos.core.errors import PreconditionFailed
    from analystos.db.models import Definition
    from analystos.services import definitions as defs

    spec = G.tenant_spec(seed=task.seed)
    with env.control() as s:
        ws, (alice, bob) = _people(s, "editor", "editor")
        row = defs.create_draft(s, alice, ws.id, DefinitionDraftIn(kind="ml_spec", key=f"heldout_{task.seed}", spec=spec))
        base = row.revision
        defs.update_draft(s, alice, row, DefinitionPatch(spec={**spec, "search": {"max_trials": 6, "max_seconds": 120}}), base)
        try:
            defs.update_draft(s, bob, s.get(Definition, row.id), DefinitionPatch(spec={**spec, "seed": 99}), base)
            stale = None
        except PreconditionFailed as exc:
            stale = exc.message
        current = s.get(Definition, row.id)
        if stale is None:
            final = dict(current.spec)
            return {"outcome": "delivered", "ok": False, "wrong": True,
                    "reason": f"the stale save was accepted; the other editor's change was lost: {final.get('search')}"}
        defs.update_draft(s, bob, current, DefinitionPatch(spec={**current.spec, "seed": 99}), current.revision)
        final = dict(s.get(Definition, row.id).spec)
    ok = final.get("seed") == 99 and (final.get("search") or {}).get("max_trials") == 6
    return {"outcome": "delivered", "ok": ok, "detail": {"stale": stale[:200]},
            "reason": "the stale save was refused; after reloading both edits survive" if ok else f"final spec lost an edit: {final}"}


def _changed_tills(seed: int) -> dict[str, dict[str, Any]]:
    """The same feed after an upstream correction: the last receipt removed and one price changed."""
    tables = json.loads(json.dumps(G.tills(seed)))
    lines = tables[f"{S}.till_lines"]["rows"]
    last = lines[-1][0]
    tables[f"{S}.till_lines"]["rows"] = [r for r in lines if r[0] != last]
    tables[f"{S}.till_lines"]["rows"][0][6] = float(round(tables[f"{S}.till_lines"]["rows"][0][6] + 1.0, 2))
    return tables


@scenario("rec_pinned_snapshot_changed", "abstain", lambda seed: {**_tills_fixture(seed), "changed": _changed_tills(seed)})
def rec_pinned_snapshot_changed(env: Any, task: Any) -> dict[str, Any]:
    """A rerun is pinned to the input snapshot of the accepted run; the feed changed since. The contract
    (`SourceNode.snapshot`): a mismatch refuses the run rather than publishing numbers from other data."""
    fx = _tills_fixture(task.seed)
    first_space = env.space("tills original", fx["tables"])
    first = env.recipe(first_space, fx["spec"], engine="duckdb")
    snaps = (first.get("detail") or {}).get("snapshots") or {}
    digest = next((d for a, d in snaps.items() if a.endswith(".till_lines") and d), None)
    if digest is None:
        raise RuntimeError(f"the first run recorded no snapshot of till_lines: {first.get('refused') or snaps}")
    pinned = json.loads(json.dumps(fx["spec"]))
    next(n for n in pinned["nodes"] if n["op"] == "source" and n["asset"].endswith(".till_lines"))["snapshot"] = digest
    second = env.recipe(env.space("tills changed", _changed_tills(task.seed)), pinned)
    if "rows" in second:
        return {"outcome": "acted", "reason": f"published {len(second['rows'])} rows from data that no longer matches the pinned "
                                              f"snapshot {digest[:12]} (engine {(second.get('detail') or {}).get('engine')})",
                "detail": {"pinned": digest}}
    if second.get("denied"):
        return {"outcome": "stopped", "kind": "denied", "reason": second["refused"], "detail": {"pinned": digest}}
    return {"outcome": "stopped", "kind": "blocked" if "blocked" in second else "refused",
            "reason": second.get("refused") or second.get("blocked"), "detail": {"pinned": digest}}


__all__ = ["SCENARIOS", "Scenario", "fixture", "run"]
