"""Governed-question slice of the Ask benchmark (P7-02 on P4-V02; ADR-0019).

Per domain: the P4-V02 setup (upload, discovery, staging, the restricted-column policy), then an
approved semantic model over the staged table: one dataset whose fields are the table's columns
(datetimes are time dimensions, text/boolean/integer columns dimensions), and the metrics of
`ask_questions/governed.yaml`, proposed by the benchmark admin and approved by a second person (the
separation of duties the platform enforces). Every question then goes through the real Ask path.

Scoring, per governed question: `governed` (labelled so), `sql_equivalent` (the SQL is exactly the
compiler's output for the labelled `query` on the same model version and scope), `match` (execution
match against the gold SQL, as the rest of the benchmark) and `deterministic` (asked again in a new
thread, the SQL is identical). A `not_governed` item passes when the answer does not carry the governed
label, whatever else happened (ad hoc answer, clarification or refusal).
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evaluation.ask import BENCH_ACTOR, DomainEnv, _admin, _sub, configure, frame_for, results_match, setup_domain

GOVERNED_FILE = Path(__file__).resolve().parent / "ask_questions" / "governed.yaml"
APPROVER_EMAIL = "approver@analystos.local"


@dataclass
class GovernedOutcome:
    id: str
    domain: str
    question: str
    expect: str  # governed | not_governed
    governance: str | None = None
    status: str | None = None
    sql: str | None = None
    expected_sql: str | None = None
    sql_equivalent: bool | None = None
    match: bool | None = None
    match_note: str = ""
    deterministic: bool | None = None
    model_calls: int = 0
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.expect == "not_governed":
            return self.error is None and self.governance != "governed"
        return bool(self.governance == "governed" and self.sql_equivalent and self.match and self.deterministic)


def load_governed() -> dict[str, Any]:
    return yaml.safe_load(GOVERNED_FILE.read_text())


def problems(spec: dict[str, Any]) -> list[str]:
    """Structure of the slice (the unit tests run it; no services)."""
    out, ids = [], []
    for domain, d in spec.items():
        names = {m["name"] for m in d["metrics"]}
        for q in d["questions"]:
            ids.append(q["id"])
            expect = q.get("expect", "governed")
            if expect not in ("governed", "not_governed"):
                out.append(f"{q['id']}: unknown expect {expect}")
            if expect == "governed" and not (q.get("query") and q.get("gold")):
                out.append(f"{q['id']}: a governed item needs the expected query and a gold SQL")
            if expect == "governed" and not set(q["query"]["metrics"]) <= names:
                out.append(f"{q['id']}: query names a metric the domain does not approve")
        if not any(q.get("expect") == "not_governed" for q in d["questions"]):
            out.append(f"{domain}: no not_governed item")
    if len(ids) != len(set(ids)):
        out.append("duplicate ids")
    return out


def _fields(frame) -> list[Any]:
    from analystos.contracts.semantic import DialectExpression, SemanticField

    out = []
    for name, dtype in frame.dtypes.items():
        kind = str(dtype)
        if kind.startswith("datetime"):
            dim: dict[str, Any] | None = {"is_time": True}
        elif kind in ("object", "str", "string", "bool") or kind.startswith("int"):
            dim = {"is_time": False}
        else:
            dim = None
        out.append(SemanticField(name=name, expressions=[DialectExpression(expression=name)], dimension=dim))
    return out


def setup_model(env: DomainEnv, spec: dict[str, Any], frame, admin) -> None:
    """The approved model: a person saves the structure, proposes each metric, another approves it."""
    from sqlalchemy import select

    from analystos.contracts.semantic import DialectExpression, SemanticDataset, SemanticMetricDef
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.semantic import service as semantic
    from analystos.services.workspaces import add_member

    with session_scope() as s:
        me = s.merge(admin)
        add_member(s, me, env.workspace_id, APPROVER_EMAIL, "approver")
        semantic.save_model(s, env.workspace_id, actor=f"user:{admin.id}", origin="user",
                            datasets=[SemanticDataset(name=env.domain, source=env.table, fields=_fields(frame))])
        approver = s.scalar(select(User).where(User.email == APPROVER_EMAIL))
        for m in spec["metrics"]:
            defn = SemanticMetricDef(name=m["name"], expressions=[DialectExpression(expression=m["expression"])],
                                     display_name=m.get("display_name"), format=m.get("format"), dataset=env.domain,
                                     dimensions=list(m.get("dimensions") or []), ai_context=m.get("ai_context"))
            semantic.propose_metric(s, env.workspace_id, defn, proposed_by=admin.id, via="user")
            semantic.decide_metric(s, env.workspace_id, m["name"], approver, approve=True, reason="governed benchmark")


def _ask(env: DomainEnv, admin, text: str, parameters: dict[str, Any] | None) -> dict[str, Any]:
    from analystos.db.base import session_scope
    from analystos.services.ask import ask_in_thread, create_thread

    with session_scope() as s:
        thread_id = create_thread(s, s.merge(admin), env.workspace_id, title="governed benchmark")["id"]
    return ask_in_thread(admin, thread_id, text, parameters)


def expected_sql(env: DomainEnv, admin, query: dict[str, Any]) -> str:
    """What the compiler produces for the labelled query, on the current approved model and scope."""
    from analystos.contracts.semantic import SemanticQuery
    from analystos.db.base import session_scope
    from analystos.governance.policy import resolve_scope
    from analystos.semantic.compiler import compile_query, load_catalog

    with session_scope() as s:
        scope = resolve_scope(s, s.merge(admin), env.workspace_id)
        catalog = load_catalog(s, env.workspace_id)
    return compile_query(SemanticQuery.model_validate(query), catalog, scope).sql


def ask_governed(q: dict[str, Any], domain: str, env: DomainEnv, admin, gold: dict[str, list]) -> GovernedOutcome:
    from evaluation.ask import _turn_usage

    out = GovernedOutcome(id=q["id"], domain=domain, question=q["q"], expect=q.get("expect", "governed"))
    try:
        turn = _ask(env, admin, q["q"], q.get("parameters"))
        out.governance, out.status, out.sql = turn.get("governance"), turn.get("status"), turn.get("sql")
        out.model_calls = _turn_usage(turn["id"], env.workspace_id)["model_calls"]
        if out.expect == "governed":
            out.expected_sql = expected_sql(env, admin, q["query"])
            out.sql_equivalent = out.sql == out.expected_sql
            result = turn.get("result") or {}
            out.match, out.match_note = results_match(gold[q["id"]], result.get("rows") or []) if result else (False, "no result")
            again = _ask(env, admin, q["q"], q.get("parameters"))
            out.deterministic = again.get("sql") == out.sql and again.get("governance") == "governed"
    except Exception as exc:  # noqa: BLE001 - an outcome of the harness, reported per question
        out.error = f"{exc.__class__.__name__}: {exc}"[:300]
    return out


@dataclass
class GovernedRun:
    tier: str
    outcomes: list[GovernedOutcome]
    summary: dict[str, Any]
    seconds: float
    setup: dict[str, Any] = field(default_factory=dict)


def summarize(outcomes: list[GovernedOutcome]) -> dict[str, Any]:
    gov = [o for o in outcomes if o.expect == "governed"]
    neg = [o for o in outcomes if o.expect == "not_governed"]

    def share(xs: list[bool | None]) -> float | None:
        return round(sum(bool(x) for x in xs) / len(xs), 4) if xs else None

    return {"questions": len(outcomes), "governed_items": len(gov), "not_governed_items": len(neg),
            "governed_rate": share([o.governance == "governed" for o in gov]),
            "sql_equivalence": share([o.sql_equivalent for o in gov]), "execution_accuracy": share([o.match for o in gov]),
            "deterministic": share([o.deterministic for o in gov]),
            "false_governed": sum(o.governance == "governed" for o in neg),
            "model_calls": sum(o.model_calls for o in outcomes), "errors": sum(o.error is not None for o in outcomes),
            "passed": sum(o.passed for o in outcomes)}


def run_governed(tier: str = "off", *, domains: tuple[str, ...] | None = None, seed: int = 1) -> GovernedRun:
    """The slice on one tier (default `off`: no provider, the rules rung and the compiler alone)."""
    from analystos.db.base import session_scope
    from analystos.governance.policy import resolve_scope
    from analystos.runtime.context import default_gateway

    started = time.perf_counter()
    spec = load_governed()
    restore, oracle = configure(tier)
    try:
        admin, gateway = _admin(), default_gateway()
        outcomes, setup = [], {}
        for domain in domains or tuple(spec):
            _, frame, qs = frame_for(domain, seed)
            env = setup_domain(qs, frame, admin, seed=seed)
            setup_model(env, spec[domain], frame, admin)
            with session_scope() as s:
                scope = resolve_scope(s, s.merge(admin), env.workspace_id)
                s.expunge_all()
            gold = {q["id"]: gateway.execute(scope, _sub(q["gold"], env.table), actor=BENCH_ACTOR, purpose="evaluation.gold",
                                             use_cache=False).rows
                    for q in spec[domain]["questions"] if q.get("gold")}
            if oracle is not None:
                oracle.answers = {q["q"]: _sub(q.get("gold") or "", env.table) or None for q in spec[domain]["questions"]}
            setup[domain] = {"workspace_id": env.workspace_id, "table": env.table, "metrics": len(spec[domain]["metrics"])}
            outcomes += [ask_governed(q, domain, env, admin, gold) for q in spec[domain]["questions"]]
        return GovernedRun(tier, outcomes, summarize(outcomes), round(time.perf_counter() - started, 1), setup)
    finally:
        restore()


def as_dict(r: GovernedRun) -> dict[str, Any]:
    return {"tier": r.tier, "seconds": r.seconds, "setup": r.setup, "summary": r.summary,
            "outcomes": [{**asdict(o), "passed": o.passed} for o in r.outcomes]}
