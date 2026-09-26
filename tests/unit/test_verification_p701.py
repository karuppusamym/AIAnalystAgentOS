"""P7-01 (ADR-0020): verification records bound to a dependency fingerprint, voided by the change that
breaks them, swept nightly; consumers (publish gate, reports, export, learning) read the record's state;
and P7-08 "why this number?" resolves a displayed number link by link. SQLite control plane, no services.

Acceptance cases: editing the step's SQL/spec, approving a new version of a KPI the verdict measured, a
snapshot change, an edit of a cited glossary entry and a method version bump each void the verdict; the
sweep catches a change whose event was missed; the publish gate and reports refuse to present a VOID."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from analystos import methods
from analystos.agents.insight import template_text
from analystos.core.errors import InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import stable_hash
from analystos.evidence import manifest as M
from analystos.evidence import verification as V
from analystos.evidence.facts import facts_from

ROOT = Path(__file__).resolve().parents[2]
WS, RUN, SRC, ASSET = "ws_p701", "run_p701", "src_p701", "shop.orders"
SPEC = {"method": "rate_by_segment", "asset": ASSET,
        "outcome": {"type": "equals", "column": "made_sla", "value": False, "label": "missed SLA"},
        "segment": {"type": "column", "column": "priority", "label": "priority"}}
STAT = {"test": "chi_square_independence", "n": 20000, "p_value": 1e-14, "p_adjusted": 1e-12, "effect_size": 0.21,
        "effect_label": "cramers_v",
        "highlights": {"top_segment": "P1", "top_rate": 0.412, "top_n": 2000, "baseline_segment": "P4",
                       "baseline_rate": 0.131, "baseline_n": 9000, "rate_ratio": 3.14, "n_groups": 4},
        "groups": [{"segment": s, "n": n} for s, n in (("P1", 2000), ("P2", 4000), ("P3", 5000), ("P4", 9000))]}
EXTRA = ("experiment", "semantic_metric", "semantic_model", "context_entry", "knowledge_document", "knowledge_suggestion",
         "schedule", "schedule_run", "publication", "feedback", "decision")


@pytest.fixture
def db(sqlite_db):
    from analystos.db import models

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in EXTRA
                                                    if t in models.Base.metadata.tables])
    return sqlite_db


def _insight(s, *, n: int = 1, spec: dict | None = None, metric: bool = True, term: bool = True):
    """A verified finding on a staged snapshot, with a KPI and a glossary term on its outcome column."""
    from analystos.db.models import (
        AnalysisRun,
        Artifact,
        ContextEntry,
        Experiment,
        Hypothesis,
        Insight,
        QueryExecution,
        SemanticMetric,
        Source,
        SourceAsset,
        User,
        Workspace,
    )

    spec = dict(spec or SPEC)
    if s.get(Workspace, WS) is None:
        s.add(User(id="u_owner", email="owner@p701.test", name="Owner", password_hash="x"))
        s.add(Workspace(id=WS, name="p701", created_by="u_owner", policy_version=0))
        s.add(Source(id=SRC, workspace_id=WS, kind="postgres", name="shop", execution_mode="staged"))
        s.add(SourceAsset(id="asset_p701", source_id=SRC, workspace_id=WS, schema_name="shop", name="orders",
                          source_name="orders", row_count=20000,
                          snapshot={"content_fingerprint": "c1", "rows_staged": 20000, "load_id": "l1", "sampling_method": "full"}))
        s.flush()
        entry = M.current_entry(s, ASSET, SRC)
        s.add(AnalysisRun(id=RUN, workspace_id=WS, objective="SLA", requested_by="u_owner", status="COMPLETED",
                          data_manifest=M.build([entry]).model_dump(mode="json")))
        if metric:
            s.add(SemanticMetric(id="smet_1", workspace_id=WS, name="missed_sla_rate", version=1, status="approved",
                                 definition={"name": "missed_sla_rate", "source_columns": ["made_sla"],
                                             "expressions": [{"dialect": "ANSI_SQL", "expression": "x"}]},
                                 expression='AVG(CASE WHEN NOT "made_sla" THEN 1.0 ELSE 0.0 END)',
                                 normalized_expression="avg(...)", proposed_by="u_owner", proposed_via="user", content_hash="m1"))
        if term:
            s.add(ContextEntry(id="ctx_sla", workspace_id=WS, kind="term", name="Made SLA", body="Resolved within the SLA.",
                               synonyms=[], mapped_columns=["made_sla"]))
            s.add(Artifact(id="art_ctx", workspace_id=WS, run_id=RUN, type="context_package", name="Context package",
                           content={"resolved_terms": [{"id": "ctx_sla", "term": "Made SLA", "columns": ["made_sla"]}]},
                           content_hash="x"))
        s.flush()
    entry = M.current_entry(s, ASSET, SRC)
    s.add(Hypothesis(id=f"hyp_{n}", workspace_id=WS, run_id=RUN, code=f"H-{n}", statement="SLA misses by priority",
                     spec=spec, status="supported"))
    s.add(QueryExecution(id=f"q_{n}", workspace_id=WS, source_id=SRC, run_id=RUN, actor="agent:analyst",
                         sql=f"SELECT priority, avg(made_sla::int) FROM shop.orders GROUP BY 1 -- {n}", status="ok",
                         result_hash=f"h{n}", row_count=4))
    s.add(Experiment(id=f"exp_{n}", workspace_id=WS, run_id=RUN, hypothesis_id=f"hyp_{n}", method=spec["method"], params={},
                     result=STAT, query_ids=[f"q_{n}"], role="primary"))
    facts = facts_from(spec, STAT, query_ids=(f"q_{n}",), result_hashes=(f"h{n}",))
    title, finding = template_text(STAT, spec)
    bundle = {"version": "evidence.v1", "data": {"entry": entry.model_dump(mode="json"),
                                                 "manifest": M.build([entry]).model_dump(mode="json"),
                                                 "queries": [{"query_id": f"q_{n}", "result_hash": f"h{n}"}]},
              "claim": {"facts": [f.model_dump(mode="json") for f in facts]}}
    ins = Insight(id=f"ins_{n}", workspace_id=WS, run_id=RUN, hypothesis_id=f"hyp_{n}", code=f"I-{n}", title=title,
                  finding=finding, confidence=0.8, status="verified", verified=True, evidence_bundle=bundle,
                  evidence=[{"type": "query", "id": f"q_{n}"}], validation="exploratory", data_version="dv")
    s.add(ins)
    s.flush()
    from analystos.registries.hypotheses import spec_hash

    deps = V.insight_dependencies(s, workspace_id=WS, run_id=RUN, hypothesis_id=f"hyp_{n}", spec=spec, entry=entry,
                                  narrative_source="template")
    rec = V.record_verdict(s, workspace_id=WS, run_id=RUN, subject_type="insight", subject_id=ins.id, verdict="verified",
                           checks=[{"check": "sample_size", "passed": True}], verifier="rev.v2", dependencies=deps,
                           question_hash=spec_hash(spec), evidence_bundle=bundle)
    return ins, rec


def _state(s, rec_id):
    from analystos.db.models import VerificationRecord

    return s.get(VerificationRecord, rec_id)


# ------------------------------------------------------------------------------------ the record
def test_record_binds_every_dependency_kind_into_the_fingerprint(db):
    with db() as s:
        _, rec = _insight(s)
        kinds = {d["kind"] for d in rec.dependencies}
        assert kinds == {"query", "data", "semantic", "method", "context", "policy"}
        assert rec.state == "ACTIVE" and rec.verdict == "verified" and rec.verifier == "rev.v2"
        assert rec.fingerprint == stable_hash(sorted(rec.dependencies, key=lambda d: (d["kind"], d["ref"], d["version_hash"])))
        data = next(d for d in rec.dependencies if d["kind"] == "data")
        assert data["ref"] == f"{SRC}/{ASSET}" and data["version_hash"] == M.current_entry(s, ASSET, SRC).version
        assert next(d for d in rec.dependencies if d["kind"] == "semantic")["ref"] == f"{WS}/missed_sla_rate"
        assert next(d for d in rec.dependencies if d["kind"] == "context")["ref"] == "ctx_sla"
        # every recorded version is today's version: nothing to void
        assert V.recheck(s)["voided"] == []


def test_a_new_verdict_supersedes_the_live_one_and_the_old_stays_readable(db):
    with db() as s:
        ins, first = _insight(s)
        second = V.record_verdict(s, workspace_id=WS, run_id=RUN, subject_type="insight", subject_id=ins.id,
                                  verdict="verified", checks=[], verifier="rev.v2", dependencies=[])
        assert (first.state, first.superseded_by, second.state) == ("SUPERSEDED", second.id, "ACTIVE")
        assert V.latest(s, "insight", [ins.id])[ins.id].id == second.id


# ------------------------------------------------------------------------------------ acceptance: each change voids
def test_editing_the_steps_sql_voids_the_verdict(db):
    from analystos.db.models import Hypothesis

    with db() as s:
        _, rec = _insight(s)
        before = V.current_version(s, "query", "hyp_1")
        s.get(Hypothesis, "hyp_1").spec = {**SPEC, "filters": [{"column": "channel", "op": "=", "value": "web"}]}
        s.flush()
        assert V.current_version(s, "query", "hyp_1") != before
        voided = V.dependency_changed(s, "query", "hyp_1", "step H-1 edited", event="step.edited")
        assert voided == [rec.id]
        rec = _state(s, rec.id)
        assert (rec.state, rec.void_kind, rec.void_reason) == ("VOID", "query", "step H-1 edited")
        assert rec.void_detail["event"] == "step.edited" and rec.void_detail["late"] is False


def test_void_dependents_is_public_and_only_voids_other_versions(db):
    with db() as s:
        _, rec = _insight(s)
        same = next(d for d in rec.dependencies if d["kind"] == "query")["version_hash"]
        assert V.void_dependents(s, "query", "hyp_1", same, "no change") == []
        assert V.void_dependents(s, "query", "hyp_1", V.UNKNOWABLE, "unknowable") == []
        assert V.void_dependents(s, "query", "hyp_1", "v2", "step edited (version 2)") == [rec.id]
        assert V.void_dependents(s, "query", "hyp_1", "v3", "again") == []  # a void is not voided twice
        with pytest.raises(InvalidInput):
            V.void_dependents(s, "sql", "hyp_1", "v2", "unknown kind")


def test_approving_a_new_metric_version_voids_the_verdict(db, monkeypatch):
    """The real approval path: apply_decision flips the approved version and calls the hook."""
    from datetime import timedelta

    from analystos.core.ids import utcnow
    from analystos.db.models import Approval, SemanticMetric
    from analystos.governance import approvals
    from analystos.knowledge import learning
    from analystos.semantic import service as semantic

    with db() as s:
        _, rec = _insight(s)
        s.add(SemanticMetric(id="smet_2", workspace_id=WS, name="missed_sla_rate", version=2, status="proposed",
                             definition={"name": "missed_sla_rate", "expressions": [{"dialect": "ANSI_SQL", "expression": "y"}]},
                             expression="y", normalized_expression="y", proposed_by="u_owner", proposed_via="user",
                             content_hash="m2", approval_id="apr_2"))
        s.add(Approval(id="apr_2", workspace_id=WS, action=semantic.APPROVAL_ACTION, payload={}, payload_hash="p",
                       policy_version=0, requested_by="u_owner", status="approved", decided_by="u_approver",
                       risk_tier="medium", expires_at=utcnow() + timedelta(hours=1)))
        s.flush()
        monkeypatch.setattr(approvals, "verify_for_execution", lambda *a, **k: None)
        monkeypatch.setattr(learning, "draft_from_metric", lambda *a, **k: None)
        semantic.apply_decision(s, s.get(Approval, "apr_2"))
        rec = _state(s, rec.id)
        assert (rec.state, rec.void_kind) == ("VOID", "semantic")
        assert rec.void_reason == "metric missed_sla_rate v2 approved"


def test_deprecating_the_metric_voids_the_verdict(db):
    from analystos.semantic import service as semantic

    with db() as s:
        _, rec = _insight(s)
        semantic.deprecate_metric(s, WS, "missed_sla_rate", SimpleNamespace(id="u_owner"), reason="retired")
        assert (_state(s, rec.id).state, _state(s, rec.id).void_kind) == ("VOID", "semantic")


def test_a_snapshot_change_voids_the_verdict_as_the_data_case(db):
    """P4-03 `stale` is the data case of VOID: mark_stale voids in the same transaction."""
    from analystos.db.models import Insight, SourceAsset

    with db() as s:
        ins, rec = _insight(s)
        s.get(SourceAsset, "asset_p701").snapshot = {"content_fingerprint": "c2", "rows_staged": 19000, "load_id": "l2",
                                                     "sampling_method": "full"}
        s.flush()
        M.mark_stale(s, WS, SRC, ASSET)
        rec = _state(s, rec.id)
        assert (rec.state, rec.void_kind) == ("VOID", "data") and "shop.orders" in rec.void_reason
        assert s.get(Insight, ins.id).stale_since is not None  # the old field still set for compatibility


def test_an_identical_restage_voids_nothing(db):
    from analystos.db.models import SourceAsset

    with db() as s:
        _, rec = _insight(s)
        s.get(SourceAsset, "asset_p701").snapshot = {"content_fingerprint": "c1", "rows_staged": 20000, "load_id": "l9",
                                                     "sampling_method": "full"}
        s.flush()
        M.mark_stale(s, WS, SRC, ASSET)
        assert _state(s, rec.id).state == "ACTIVE"


def test_an_edit_to_a_cited_glossary_entry_voids_the_verdict(db):
    from analystos.db.models import ContextEntry

    with db() as s:
        _, rec = _insight(s)
        s.get(ContextEntry, "ctx_sla").body = "Resolved within the contractual SLA, excluding holidays."
        out = V.recheck(s, kinds=("context",), reason="knowledge revision", event="knowledge.section_changed")
        assert out["voided"] == [rec.id]
        assert (_state(s, rec.id).void_kind, _state(s, rec.id).void_detail["event"]) == ("context", "knowledge.section_changed")


def test_a_method_version_bump_voids_the_verdict(db, monkeypatch):
    with db() as s:
        _, rec = _insight(s)
        reg = methods.current()
        bumped = dict(reg.manifests)
        bumped["rate_by_segment"] = reg.manifests["rate_by_segment"].model_copy(update={"version": "9.0.0"})
        monkeypatch.setattr(reg, "manifests", bumped)
        out = V.recheck(s, kinds=("method",), reason="capability registry reloaded", event="method.version_changed")
        assert out["voided"] == [rec.id] and out["by_kind"] == {"method": 1}
        assert _state(s, rec.id).void_kind == "method"


def test_a_policy_change_voids_only_when_a_verdict_relevant_field_changed(db):
    from analystos.contracts.policy import WorkspacePolicyDoc
    from analystos.db.models import Workspace
    from analystos.governance.policy import save_policy

    with db() as s:
        _, rec = _insight(s)
        ws = s.get(Workspace, WS)
        save_policy(s, ws, WorkspacePolicyDoc(run_token_budget=123_000), "u_owner")  # budgets do not shape a verdict
        assert _state(s, rec.id).state == "ACTIVE"
        save_policy(s, ws, WorkspacePolicyDoc(run_token_budget=123_000, alpha=0.01), "u_owner")
        assert (_state(s, rec.id).state, _state(s, rec.id).void_kind) == ("VOID", "policy")


def test_the_sweep_catches_a_change_whose_event_was_missed_and_counts_it(db):
    from analystos.db.models import SourceAsset, VerificationSweep

    with db() as s:
        _, rec = _insight(s)
        _, other = _insight(s, n=2, spec={**SPEC, "segment": {"type": "column", "column": "channel", "label": "channel"}})
        # a re-stage that bypassed mark_stale: the event path is missing
        s.get(SourceAsset, "asset_p701").snapshot = {"content_fingerprint": "c3", "rows_staged": 1, "load_id": "l3",
                                                     "sampling_method": "full"}
        s.flush()
        out = V.sweep(s, actor="test")
        assert out["checked"] == 2 and out["late_voids"] == 2 and out["by_kind"] == {"data": 2}
        assert {_state(s, rec.id).state, _state(s, other.id).state} == {"VOID"}
        assert _state(s, rec.id).void_detail["late"] is True
        row = s.get(VerificationSweep, out["sweep_id"])
        assert (row.checked, row.late_voids) == (2, 2)
        assert V.sweep(s, actor="test")["late_voids"] == 0  # nothing live is left to miss


def test_the_nightly_sweep_runs_once_a_day(db):
    from datetime import timedelta

    from analystos.core.ids import utcnow

    with db() as s:
        _insight(s)
        now = utcnow()
        assert V.maybe_sweep_nightly(s, now=now) is not None
        s.flush()
        assert V.maybe_sweep_nightly(s, now=now + timedelta(hours=1)) is None
        assert V.maybe_sweep_nightly(s, now=now + timedelta(hours=25)) is not None


def test_a_replan_supersedes_the_runs_verdicts(db):
    with db() as s:
        ins, rec = _insight(s)
        assert V.supersede_subjects(s, "insight", [ins.id], "replanned: redirect") == 1
        assert _state(s, rec.id).state == "SUPERSEDED"
        assert V.recheck(s)["dependencies"] == 0  # superseded verdicts are not checked or voided any more


# ------------------------------------------------------------------------------------ consumers
def test_publish_gate_refuses_a_void_finding_with_its_cause(db):
    with db() as s:
        ins, rec = _insight(s)
        V.gate_publish(s, RUN, [ins.code])  # ACTIVE: allowed
        V.void_dependents(s, "data", f"{SRC}/{ASSET}", "new", "data snapshot shop.orders changed")
        with pytest.raises(PolicyDenied) as exc:
            V.gate_publish(s, RUN, [ins.code, "I-99"])
        assert "I-1 is void (data): data snapshot shop.orders changed" in exc.value.message
        assert exc.value.details["void_findings"][0]["record_id"] == rec.id


def test_reports_never_present_a_void_finding_as_verified(db):
    from analystos.reports import render
    from analystos.services.reports import build_report_data

    with db() as s:
        ins, _ = _insight(s)
        _insight(s, n=2, spec={**SPEC, "segment": {"type": "column", "column": "channel", "label": "channel"}})
        V.void_dependents(s, "query", "hyp_1", "edited", "step H-1 edited")
        data = build_report_data(s, RUN)
        by_code = {i.code: i for i in data.insights}
        assert by_code["I-1"].verified is False and by_code["I-1"].void_reason == "query: step H-1 edited"
        assert by_code["I-2"].verified is True and by_code["I-2"].void_reason is None
        md = render(data, "md")[0].decode()
        assert "_void, discovery (exploratory); VOID: query: step H-1 edited; needs re-verification" in md
        html = render(data, "html")[0].decode()
        assert "VOID: query: step H-1 edited" in html
        from analystos.reports._common import top_verified

        assert [i.code for i in top_verified(data.insights)] == ["I-2"]  # a void is shown, never counted verified


def test_export_and_learning_refuse_a_void_finding(db):
    from analystos.knowledge.attested import attested_from_insight
    from analystos.knowledge.learning import draft_from_finding

    with db() as s:
        ins, _ = _insight(s)
        V.void_dependents(s, "method", "rate_by_segment", "other", "method rate_by_segment changed")
        with pytest.raises(InvalidInput, match="VOID"):
            attested_from_insight(s, ins)
        assert draft_from_finding(s, ins, user_id="u_owner") is None


# ------------------------------------------------------------------------------------ presentation rules
def test_a_void_is_shown_with_its_cause(db):
    with db() as s:
        ins, rec = _insight(s)
        V.void_dependents(s, "context", "ctx_sla", "edited", "knowledge ctx_sla changed")
        state = V.insight_view(s, ins)
        assert state["state"] == "VOID" and state["badge"] == "void"
        assert state["void"]["kind"] == "context" and state["void"]["reason"] == "knowledge ctx_sla changed"
        assert V.void_cause(state) == "context: knowledge ctx_sla changed"


def test_a_prior_verdict_on_the_same_question_is_offered_not_applied(db):
    with db() as s:
        first, first_rec = _insight(s)
        V.void_dependents(s, "query", "hyp_1", "edited", "step H-1 edited")
        second, second_rec = _insight(s, n=2)  # the same AnalysisSpec asked again
        view = V.insight_view(s, second)
        assert view["record_id"] == second_rec.id and view["state"] == "ACTIVE"  # its own verdict, never the old one
        assert [(p["subject_id"], p["state"], p["offered"], p["applied"]) for p in view["prior_verdicts"]] == \
            [(first.id, "VOID", True, False)]
        assert view["prior_verdicts"][0]["void"]["reason"] == "step H-1 edited"


def test_flagging_a_finding_wrong_requires_a_reason_kept_with_the_record(db):
    with db() as s:
        ins, rec = _insight(s)
        for empty in (None, "", "   "):
            with pytest.raises(InvalidInput, match="reason"):
                V.flag_wrong(s, ins, user_id="u_owner", reason=empty)
        flag = V.flag_wrong(s, ins, user_id="u_owner", reason="P1 tickets were re-labelled in March")
        assert flag["record_id"] == rec.id
        assert _state(s, rec.id).flags == [{"by": "user:u_owner", "reason": "P1 tickets were re-labelled in March",
                                            "at": flag["at"]}]


def test_finding_outcome_wrong_needs_a_reason_and_drafts_negative_knowledge(db, monkeypatch):
    from analystos.api.routers import artifacts, decisions
    from analystos.db.models import User, WorkspaceMember
    from analystos.knowledge import learning

    drafted = []
    monkeypatch.setattr(learning, "draft_from_flag", lambda s, ins, **k: drafted.append((ins.id, k["reason"])) or None)
    with db() as s:
        ins, rec = _insight(s)
        s.add(WorkspaceMember(workspace_id=WS, user_id="u_owner", role="owner"))
        s.flush()
        owner = s.get(User, "u_owner")
        with pytest.raises(InvalidInput, match="reason"):
            decisions.finding_outcome(ins.id, decisions.FindingOutcomeIn(signal="wrong"), owner, s)
        out = decisions.finding_outcome(ins.id, decisions.FindingOutcomeIn(signal="wrong", reason="wrong population"), owner, s)
        assert out["flag"]["reason"] == "wrong population" and drafted == [(ins.id, "wrong population")]
        got = artifacts.get_insight(ins.id, owner, s)["verification_state"]
        assert got["flags"][0]["reason"] == "wrong population" and got["state"] == "ACTIVE"
        assert got["record_id"] == rec.id


# ------------------------------------------------------------------------------------ P7-08: why this number?
def test_every_displayed_number_resolves_through_six_links(db):
    from analystos.evidence.why import LINKS, explain_insight

    with db() as s:
        ins, rec = _insight(s)
        out = explain_insight(s, ins)
        assert out["numbers"] and out["state"] == "ok"
        for n in out["numbers"]:
            assert [lk["link"] for lk in n["links"]] == list(LINKS)
            assert n["state"] == "ok", n
            states = {lk["link"]: lk["state"] for lk in n["links"]}
            assert states["semantic_version"] == "ok" and states["verdict"] == "ok"
        receipt = out["numbers"][0]["links"][2]["detail"]["queries"][0]
        assert (receipt["query_id"], receipt["kind"], receipt["result_hash"]) == ("q_1", "sql", "h1")
        assert out["numbers"][0]["links"][5]["detail"]["record_id"] == rec.id
        one = explain_insight(s, ins, number=out["numbers"][0]["text"])
        assert len(one["numbers"]) >= 1 and one["numbers"][0]["text"] == out["numbers"][0]["text"]
        with pytest.raises(NotFound):
            explain_insight(s, ins, number="987654")


def test_a_voided_or_broken_link_is_shown_never_hidden(db):
    from analystos.db.models import QueryExecution, SourceAsset
    from analystos.evidence.why import explain_insight

    with db() as s:
        ins, _ = _insight(s)
        s.get(SourceAsset, "asset_p701").snapshot = {"content_fingerprint": "c2", "rows_staged": 5, "load_id": "l2"}
        s.flush()
        M.mark_stale(s, WS, SRC, ASSET)
        s.get(QueryExecution, "q_1").result_hash = "tampered"
        s.flush()
        out = explain_insight(s, ins)
        n = out["numbers"][0]
        states = {lk["link"]: lk["state"] for lk in n["links"]}
        assert states == {"fact": "ok", "step": "ok", "query_receipt": "broken", "data_version": "changed",
                          "semantic_version": "ok", "verdict": "void"}
        assert n["state"] == "broken" and out["state"] == "broken"
        verdict = n["links"][5]
        assert verdict["reason"].startswith("data: data snapshot shop.orders changed")
        assert "result hash differs" in n["links"][2]["reason"]


def test_every_number_of_a_run_report_resolves(db):
    from analystos.evidence.why import explain_run

    with db() as s:
        _insight(s)
        _insight(s, n=2, spec={**SPEC, "segment": {"type": "column", "column": "channel", "label": "channel"}})
        out = explain_run(s, RUN)
        assert [f["subject"]["code"] for f in out["findings"]] == ["I-1", "I-2"]
        assert out["numbers"] > 0 and out["by_state"] == {"ok": out["numbers"]}


# ------------------------------------------------------------------------------------ migration 0032
def _migration():
    path = ROOT / "migrations" / "versions" / "0032_verification_records.py"
    spec = importlib.util.spec_from_file_location("mig0032", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_maps_stale_to_void_data_verified_to_active_and_legacy():
    mig = _migration()
    entry = {"asset": ASSET, "source_id": SRC, "mode": "staged", "version": "v1"}
    state, deps, void = mig.mapped_record({"evidence_bundle": {"version": "evidence.v1", "data": {"entry": entry}},
                                           "stale_since": None})
    assert (state, void) == ("ACTIVE", None)
    assert deps == [{"kind": "data", "ref": f"{SRC}/{ASSET}", "version_hash": "v1"}]
    assert mig.fingerprint(deps) == V.fingerprint([V.Dependency("data", f"{SRC}/{ASSET}", "v1")])  # the copy does not drift
    state, _, void = mig.mapped_record({"evidence_bundle": {"version": "evidence.v1", "data": {"entry": entry},
                                                            "freshness": {"reason": "snapshot of shop.orders changed"}},
                                        "stale_since": "2026-09-26T00:00:00Z"})
    assert (state, void["kind"], void["reason"]) == ("VOID", "data", "snapshot of shop.orders changed")
    assert mig.mapped_record({"evidence_bundle": {"version": "evidence.legacy", "data": {}}, "stale_since": None})[0] == "LEGACY"
    assert (mig.revision, mig.down_revision) == ("0032", "0030")
