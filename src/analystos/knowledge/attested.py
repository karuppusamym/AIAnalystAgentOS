"""Findings as OKF v0.2 Attested Computations (P4-K04, spec v3 §6.1, OKF §10).

A verified finding becomes `findings/<insight-id>.md`, `type: Attested Computation`. The OKF §10.2
contract fields say how a consumer re-runs and checks it; the `analystos.attestation` mapping (a
producer extension, §4.1) carries the evidence the REV already holds:

| field | from |
|---|---|
| `runtime` | the gateway dialect of the primary query's source |
| `executor` | the governed gateway; receipt = query id, executed SQL, result hash |
| `attester` | the REV reproducible re-run (identical result hash) |
| `method`, `params`, `spec_hash` | the hypothesis's AnalysisSpec and its registry hash |
| `plan_hash`, `run_id` | the analysis run's approved plan |
| `query_hash`, `result_hash`, `queries` | `query_execution.fingerprint` / `.result_hash` of every query behind it |
| `statistics.q_value`, `effect_size` | the primary experiment (BH-adjusted p is the q-value) |
| `verified_by` | the REV checks (`process:analystos-rev`) and any human approver (`human:<id>`) |
| `stale_after` (OKF §5.5) | verification time + `stale_days` |

Nothing here executes anything: the document records a computation and the means to check it (OKF
§10 "does not execute anything itself"); import counts Attested Computations and never runs them.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput, NotFound
from analystos.knowledge import okf

TYPE = "Attested Computation"
REV_ACTOR = "process:analystos-rev"
EXECUTOR = "analystos://gateway/QueryGateway.execute"
ATTESTER = "analystos://rev/reproducible_rerun"
RECEIPT = ["query_id", "executed_sql", "result_hash"]
DEFAULT_STALE_DAYS = 90
_STAT_KEYS = ("test", "n", "p_value", "effect_size", "effect_label")


class QueryReceipt(BaseModel):
    """One governed query behind the finding: its identity, statement hash and result hash."""

    model_config = ConfigDict(extra="forbid")
    query_id: str
    role: Literal["primary", "verification"] = "primary"
    query_hash: str | None = None
    result_hash: str | None = None
    source_id: str | None = None
    row_count: int = 0


class Attestor(BaseModel):
    """An OKF §7 actor and when it confirmed the finding."""

    model_config = ConfigDict(extra="forbid")
    by: str
    at: datetime
    basis: str = ""


class Statistics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test: str | None = None
    n: int | None = None
    p_value: float | None = None
    q_value: float | None = None
    effect_size: float | None = None
    effect_label: str | None = None


class AttestedComputation(BaseModel):
    """A verified finding in the OKF Attested Computation shape. `render()` writes the document;
    `from_document()` reads one back (round trip)."""

    model_config = ConfigDict(extra="forbid")
    insight_id: str
    code: str = ""
    workspace_id: str
    run_id: str
    title: str
    claim: str
    status: Literal["draft", "stable", "deprecated"] = "draft"
    runtime: str
    computation: str = ""  # the executed SQL of the primary query
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    spec_hash: str
    plan_hash: str | None = None
    query_hash: str
    result_hash: str
    queries: list[QueryReceipt]
    statistics: Statistics
    confidence: float | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)  # [{check, passed}]
    verified_by: list[Attestor]
    approved_by: list[Attestor] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
    generated_by: str = REV_ACTOR
    generated_at: datetime
    stale_after: datetime

    @property
    def q_value(self) -> float | None:
        return self.statistics.q_value

    @property
    def effect_size(self) -> float | None:
        return self.statistics.effect_size

    @property
    def path(self) -> str:
        return finding_path(self.insight_id)

    # ------------------------------------------------------------------ OKF
    def frontmatter(self) -> dict[str, Any]:
        attestors = [*self.verified_by, *self.approved_by]
        sources = [{"id": f"query-{q.query_id}", "resource": f"analystos://query/{q.query_id}",
                    "title": f"Governed query {q.query_id} ({q.role})"} for q in self.queries]
        sources += [{"id": f"asset-{i + 1}", "resource": f"analystos://asset/{a}", "title": a}
                    for i, a in enumerate(self.assets)]
        attestation = {
            "method": self.method, "params": self.params, "spec_hash": self.spec_hash, "plan_hash": self.plan_hash,
            "run_id": self.run_id, "query_hash": self.query_hash, "result_hash": self.result_hash,
            "queries": [q.model_dump() for q in self.queries],
            "statistics": self.statistics.model_dump(), "q_value": self.q_value, "effect_size": self.effect_size,
            "confidence": self.confidence, "checks": self.checks,
            "verified_by": [_actor(a) for a in self.verified_by], "approved_by": [_actor(a) for a in self.approved_by],
        }
        fm: dict[str, Any] = {
            "type": TYPE, "title": self.title, "status": self.status,
            "tags": sorted({"finding", f"method-{_tag(self.method)}"}),
            "runtime": self.runtime, "parameters": [],
            "executor": {"resource": EXECUTOR, "receipt": list(RECEIPT)},
            "attester": {"resource": ATTESTER},
            "generated": {"by": self.generated_by, "at": _iso(self.generated_at)},
            "verified": [{"by": a.by, "at": _iso(a.at)} for a in attestors],
            "stale_after": _iso(self.stale_after),
            "sources": sources,
            "analystos": {"kind": "finding", "origin": "rev", "trusted": self.status == "stable",
                          "finding": {"insight_id": self.insight_id, "code": self.code, "workspace_id": self.workspace_id,
                                      "run_id": self.run_id},
                          "caveats": list(self.caveats), "attestation": attestation},
        }
        desc = _first_sentence(self.claim)
        if desc:
            fm["description"] = desc
        return fm

    def body(self) -> str:
        s = self.statistics
        lines = ["# Claim", "", self.claim.strip(), "", "# Computation", "",
                 f"Executed through the governed gateway ({self.runtime}); receipt: query `{self.queries[0].query_id}`, "
                 f"result hash `{self.result_hash}`.", "", "```sql", self.computation.strip() or "-- (not recorded)", "```", "",
                 "# Evidence", "",
                 f"- method: `{self.method}`, spec hash `{self.spec_hash}`, plan hash `{self.plan_hash or '-'}`",
                 f"- {s.test or 'test'}: n={s.n}, p={s.p_value}, q={s.q_value}, {s.effect_label or 'effect'}={s.effect_size}",
                 f"- verified by: {', '.join(a.by for a in self.verified_by)}"
                 + (f"; approved by: {', '.join(a.by for a in self.approved_by)}" if self.approved_by else ""), ""]
        if self.checks:
            lines += ["| check | passed |", "|---|---|"] + [f"| {c.get('check')} | {bool(c.get('passed'))} |" for c in self.checks]
            lines.append("")
        if self.caveats:
            lines += ["# Caveats", ""] + [f"- {c}" for c in self.caveats]
        return "\n".join(lines)

    def render(self) -> str:
        return okf.render_document(self.frontmatter(), self.body())

    @classmethod
    def from_document(cls, text: str) -> AttestedComputation:
        meta, body = okf.parse_frontmatter("finding.md", text)
        problems = check_attested(meta)
        if problems:
            raise InvalidInput("not an AnalystOS Attested Computation: " + "; ".join(problems[:5]))
        ext = meta["analystos"]
        att, fin = ext["attestation"], ext["finding"]
        def attestors(items: Any) -> list[Attestor]:
            return [Attestor(by=str(a["by"]), at=okf.parse_instant(a["at"]), basis=str(a.get("basis") or ""))
                    for a in items or [] if isinstance(a, dict)]

        computation = ""
        if "```sql" in body:
            computation = body.split("```sql", 1)[1].split("```", 1)[0].strip()
        claim = body.split("# Claim", 1)[1].split("# Computation", 1)[0].strip() if "# Claim" in body else ""
        return cls(insight_id=fin["insight_id"], code=fin.get("code") or "", workspace_id=fin["workspace_id"], run_id=fin["run_id"],
                   title=meta["title"], claim=claim, status=meta.get("status") or "stable", runtime=meta["runtime"],
                   computation=computation, method=att["method"], params=att.get("params") or {}, spec_hash=att["spec_hash"],
                   plan_hash=att.get("plan_hash"), query_hash=att["query_hash"], result_hash=att["result_hash"],
                   queries=[QueryReceipt.model_validate(q) for q in att["queries"]],
                   statistics=Statistics.model_validate(att["statistics"]), confidence=att.get("confidence"),
                   checks=list(att.get("checks") or []), verified_by=attestors(att.get("verified_by")),
                   approved_by=attestors(att.get("approved_by")), caveats=list(ext.get("caveats") or []),
                   assets=[s["title"] for s in meta.get("sources") or [] if str(s.get("id", "")).startswith("asset-")],
                   generated_by=meta["generated"]["by"], generated_at=okf.parse_instant(meta["generated"]["at"]),
                   stale_after=okf.parse_instant(meta["stale_after"]))


# ------------------------------------------------------------------------------------ checks
_REQUIRED_ATTESTATION = ("method", "spec_hash", "query_hash", "result_hash", "q_value", "effect_size", "verified_by",
                         "queries", "statistics", "run_id")


def check_attested(meta: dict[str, Any] | None) -> list[str]:
    """Problems with a finding document's frontmatter against this profile: OKF §10.2 (`type`,
    `runtime`, `executor`, `attester`), §5.5 `stale_after`, and the AnalystOS evidence fields.
    Self-checked: OKF publishes no JSON Schema, so this is the profile as written above."""
    if not isinstance(meta, dict):
        return ["frontmatter missing"]
    out = []
    if meta.get("type") != TYPE:
        out.append(f"type is {meta.get('type')!r}, expected {TYPE!r}")
    if not str(meta.get("runtime") or "").strip():
        out.append("runtime missing (OKF §10.2 requires it)")
    if not isinstance(meta.get("executor"), dict) or not meta["executor"].get("resource"):
        out.append("executor.resource missing")
    if not isinstance(meta.get("attester"), dict) or not meta["attester"].get("resource"):
        out.append("attester.resource missing")
    if okf.parse_instant(meta.get("stale_after")) is None:
        out.append("stale_after missing or not an ISO 8601 instant")
    gen = meta.get("generated")
    if not isinstance(gen, dict) or not gen.get("by"):
        out.append("generated.by missing")
    ext = meta.get("analystos")
    att = ext.get("attestation") if isinstance(ext, dict) else None
    if not isinstance(att, dict):
        return [*out, "analystos.attestation missing"]
    out += [f"analystos.attestation.{k} missing" for k in _REQUIRED_ATTESTATION if k not in att]
    if not att.get("verified_by"):
        out.append("analystos.attestation.verified_by is empty: only verified findings are attested")
    if not isinstance((ext or {}).get("finding"), dict):
        out.append("analystos.finding missing")
    return out


# ------------------------------------------------------------------------------------ building
def finding_path(insight_id: str) -> str:
    return f"findings/{insight_id}.md"


def attested_from_insight(session: Session, insight: Any, *, approved_by: list[tuple[str, datetime]] | None = None,
                          stale_days: int = DEFAULT_STALE_DAYS, status: str | None = None) -> AttestedComputation:
    """The Attested Computation of one verified insight (an id or an `Insight` row).

    `approved_by` adds human approvers (`("human:<id>", at)`); with one the document is `stable`,
    otherwise a `draft` for the review queue. An unverified finding has nothing to attest: InvalidInput."""
    from analystos.connectors.kinds import dialect_for
    from analystos.db.models import AnalysisRun, Experiment, Hypothesis, Insight, QueryExecution, Source
    from analystos.registries.hypotheses import spec_hash

    ins = session.get(Insight, insight) if isinstance(insight, str) else insight
    if ins is None:
        raise NotFound(f"insight {insight} not found")
    if ins.status != "verified" or not ins.verified:
        raise InvalidInput(f"insight {ins.id} is {ins.status}: only a verified finding can be attested")
    h = session.get(Hypothesis, ins.hypothesis_id) if ins.hypothesis_id else None
    if h is None:
        raise InvalidInput(f"insight {ins.id} has no hypothesis: nothing to attest")
    run = session.get(AnalysisRun, ins.run_id)
    exps = list(session.scalars(select(Experiment).where(Experiment.hypothesis_id == h.id).order_by(Experiment.created_at)))
    primary = next((e for e in exps if e.role == "primary"), None)
    if primary is None or not primary.query_ids:
        raise InvalidInput(f"insight {ins.id} has no primary experiment with queries")
    verification = [e for e in exps if e.role == "verification"]
    roles = {qid: "primary" for qid in primary.query_ids}
    for e in verification:
        for qid in e.query_ids or []:
            roles.setdefault(qid, "verification")
    rows = {q.id: q for q in session.scalars(select(QueryExecution).where(QueryExecution.id.in_(list(roles)),
                                                                          QueryExecution.workspace_id == ins.workspace_id))}
    queries = [QueryReceipt(query_id=qid, role=roles[qid], query_hash=rows[qid].fingerprint, result_hash=rows[qid].result_hash,
                            source_id=rows[qid].source_id, row_count=rows[qid].row_count or 0)
               for qid in roles if qid in rows]
    first = rows.get(primary.query_ids[0])
    if first is None or not first.result_hash or not first.fingerprint:
        raise InvalidInput(f"insight {ins.id}: the primary query has no recorded statement or result hash")
    src = session.get(Source, first.source_id) if first.source_id else None
    runtime = dialect_for(src.kind, src.execution_mode) if src is not None else "postgres"
    stat = dict(primary.result or {})
    verified_at = max([e.created_at for e in verification] or [ins.created_at])
    checks = [{"check": c.get("check"), "passed": bool(c.get("passed"))} for c in (ins.verification or {}).get("evaluate") or []]
    basis = ", ".join(str(c["check"]) for c in checks if c["passed"]) or "deterministic REV checks"
    approvers = [Attestor(by=by, at=at, basis="approved") for by, at in (approved_by or [])]
    for a in approvers:
        if not a.by.startswith("human:"):
            raise InvalidInput(f"approver {a.by!r} must be a human: actor (OKF §7)")
    assets = sorted({a for q in rows.values() for a in (q.referenced_assets or [])})
    return AttestedComputation(
        insight_id=ins.id, code=ins.code, workspace_id=ins.workspace_id, run_id=ins.run_id, title=ins.title, claim=ins.finding,
        status=status or ("stable" if approvers else "draft"), runtime=runtime, computation=first.executed_sql or first.sql,
        method=str(h.spec.get("method") or primary.method), params=dict(h.spec), spec_hash=spec_hash(h.spec),
        plan_hash=run.plan_hash if run is not None else None, query_hash=first.fingerprint, result_hash=first.result_hash,
        queries=queries,
        statistics=Statistics(**{k: stat.get(k) for k in _STAT_KEYS}, q_value=stat.get("p_adjusted", stat.get("p_value"))),
        confidence=ins.confidence, checks=checks, verified_by=[Attestor(by=REV_ACTOR, at=verified_at, basis=basis)],
        approved_by=approvers, caveats=list(ins.caveats or []), assets=assets, generated_at=verified_at,
        stale_after=verified_at + timedelta(days=stale_days))


def write_findings(session: Session, run_id: str, *, author: str, stale_days: int = DEFAULT_STALE_DAYS) -> dict[str, Any]:
    """Write every verified finding of a run into its workspace pack as a draft Attested Computation
    (curated documents are kept; `knowledge/drafts.py`)."""
    from analystos.artifacts.registry import link
    from analystos.db.models import AnalysisRun, Insight
    from analystos.knowledge.drafts import write_drafts

    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise NotFound(f"run {run_id} not found")
    docs = {}
    for ins in session.scalars(select(Insight).where(Insight.run_id == run_id, Insight.status == "verified").order_by(Insight.code)):
        ac = attested_from_insight(session, ins, stale_days=stale_days)
        docs[ac.path] = ac.render()
    report = write_drafts(session, run.workspace_id, docs, author=author, reason=f"findings of run {run_id}",
                          origin="rev", meta={"run_id": run_id})
    for path in report.written:
        link(session, run.workspace_id, ("insight", path.rsplit("/", 1)[-1][:-3]), "attested_as", ("knowledge_document", path),
             run_id=run_id)
    return {"run_id": run_id, "findings": sorted(docs), **report.as_dict()}


# ------------------------------------------------------------------------------------ helpers
def _iso(dt: datetime) -> str:
    return okf.parse_instant(dt).isoformat().replace("+00:00", "Z")


def _actor(a: Attestor) -> dict[str, str]:
    return {"by": a.by, "at": _iso(a.at), "basis": a.basis}


def _tag(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "unknown"


def _first_sentence(text: str) -> str | None:
    from analystos.knowledge.entries import first_sentence

    return first_sentence(text)


__all__ = ["AttestedComputation", "Attestor", "QueryReceipt", "Statistics", "attested_from_insight", "check_attested",
           "finding_path", "write_findings"]
