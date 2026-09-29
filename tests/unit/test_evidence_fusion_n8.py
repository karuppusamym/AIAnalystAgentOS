"""N-8: structured + unstructured evidence fusion. Findings and Ask answers cite measured results and
knowledge documents as separate kinds; the numbers guard still accepts only measured numbers (a number
found only in a document is labelled document-sourced); a document claim that disagrees with measured
data is flagged, and measured data wins."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from analystos.contracts.citations import Citations, Conflict, DocumentNumber, NarrativeNumber, QuantitativeCitation
from analystos.contracts.evidence import Fact
from analystos.evidence import fusion
from analystos.skills.result_facts import numbers_bound

RULES_TEXT = ("## Targets\nThe resolution rate for Network P1 incidents is 90% under the current SLA.\n"
              "1. Escalate after 4 hours.\nReviewed in 2024.")
RECEIPT = {"id": "ksec_1", "section": "business_rules", "kind": "business_rules", "name": "Targets", "source": "pack",
           "sha256": "d" * 64, "document_id": "kdoc_sla", "path": "rules/sla.md", "anchor": "targets",
           "section_sha256": "s" * 64, "pack": "itsm", "trusted": True}


def _facts() -> list[Fact]:
    return [
        Fact(id="f_top", role="top_rate", kind="level", metric="resolution rate", value=0.45, unit="fraction",
             subject="Network", query_ids=["q1"], result_hashes=["rh1"]),
        Fact(id="f_base", role="baseline_rate", kind="level", metric="resolution rate", value=0.62, unit="fraction",
             subject="Database", query_ids=["q1"]),
        Fact(id="f_p", role="p_value_adjusted", kind="statistic", metric="p-value", value=0.003, unit="probability"),
    ]


def _doc(text: str = RULES_TEXT, receipt: dict | None = None):
    return fusion.document_citation(receipt or RECEIPT, text, heading="Targets")


# ------------------------------------------------------------------------------ contracts
def test_document_numbers_are_never_verified():
    with pytest.raises(ValidationError):
        DocumentNumber(text="90%", value=90, unit="percent", sentence="x", verified=True)
    n = DocumentNumber(text="90%", value=90, unit="percent", sentence="x")
    assert n.verified is False and n.source == "document"
    assert NarrativeNumber(text="45%", source="quantitative").verified
    assert not NarrativeNumber(text="90%", source="document", citation_id="d:x").verified


def test_citation_kinds_cannot_be_mixed():
    doc = _doc()
    q = QuantitativeCitation(id="q:q1", label="primary", query_id="q1", values=[0.45])
    with pytest.raises(ValidationError, match="document-sourced number must cite a document"):
        Citations(subject_type="insight", subject_id="i", quantitative=[q], documents=[doc],
                  narrative_numbers=[NarrativeNumber(text="90%", source="document", citation_id="q:q1")])
    with pytest.raises(ValidationError, match="quantitative number must cite a quantitative"):
        Citations(subject_type="insight", subject_id="i", quantitative=[q], documents=[doc],
                  narrative_numbers=[NarrativeNumber(text="45%", source="quantitative", citation_id=doc.id)])
    with pytest.raises(ValidationError, match="needs the governed query id"):
        QuantitativeCitation(id="q:", label="x", query_id="")


# ------------------------------------------------------------------------------ building citations
def test_document_citation_carries_sha256_receipts_and_its_numbers():
    d = _doc()
    assert (d.kind, d.path, d.anchor, d.document_sha256, d.section_sha256) == ("document", "rules/sla.md", "targets",
                                                                               "d" * 64, "s" * 64)
    # years and list markers are not claims; "4 hours" and "90%" are
    assert [(n.text, n.unit) for n in d.numbers] == [("90%", "percent"), ("4", "value")]
    assert all(n.verified is False for n in d.numbers)


def test_only_document_receipts_become_document_citations():
    catalog = {"id": "asset:t", "section": "catalog", "sha256": "x"}
    glossary_entry = {"id": "term_1", "section": "glossary", "sha256": "y"}  # a context entry: no document path
    docs = fusion.document_citations([RECEIPT, dict(RECEIPT), catalog, glossary_entry],
                                     {("kdoc_sla", "targets"): {"text": RULES_TEXT, "heading": "Targets"}})
    assert [d.id for d in docs] == ["d:kdoc_sla#targets"]
    assert docs[0].heading == "Targets" and "90%" in docs[0].excerpt


def test_quantitative_citations_from_facts_and_results():
    q = fusion.quantitative_from_facts(_facts(), [{"query_id": "q1", "role": "primary", "result_hash": "rh1", "query_hash": "qh"}])
    assert q[0].id == "q:q1" and q[0].result_hash == "rh1" and "f_top" in q[0].fact_ids and 0.45 in q[0].values
    r = fusion.quantitative_from_results([{"query_id": "q9", "result_hash": "rh9", "step": 2, "columns": ["team", "n"],
                                           "rows": [["Network", 12], ["DB", 30]]}, {"query_id": None}])
    assert [(c.id, c.step, c.values) for c in r] == [("q:q9", 2, [12.0, 30.0])]


# ------------------------------------------------------------------------------ numbers guard stays strict
def test_a_number_only_in_a_document_fails_the_numbers_guard_and_is_labelled_document():
    text = "Network resolves 45% of P1 incidents (step 1). The SLA target is 90% (step 1)."
    measured = [0.45, 0.62]
    guard = numbers_bound(text, measured, steps=[1])
    assert not guard["ok"] and any("90%" in p for p in guard["problems"])  # the guard is unchanged
    doc = _doc()
    q = QuantitativeCitation(id="q:q1", label="primary", query_id="q1", values=measured)
    labelled = {n.text: (n.source, n.citation_id) for n in fusion.label_numbers(text, [q], [doc])}
    assert labelled["45%"] == ("quantitative", "q:q1")
    assert labelled["90%"] == ("document", doc.id)


def test_a_number_both_measured_and_in_a_document_is_measured():
    doc = _doc("The resolution rate for Network is 45%.")
    q = QuantitativeCitation(id="q:q1", label="primary", query_id="q1", values=[0.45])
    assert [n.source for n in fusion.label_numbers("Network resolves 45%.", [q], [doc])] == ["quantitative"]


def test_untraceable_numbers_are_unbound():
    assert [n.source for n in fusion.label_numbers("About 17 teams.", [], [_doc()])] == ["unbound"]


# ------------------------------------------------------------------------------ conflicts
def test_a_document_claim_that_disagrees_with_measured_data_is_flagged():
    found = fusion.conflicts(fusion.measured_from_facts(_facts()), [_doc()])
    assert len(found) == 1
    c = found[0]
    assert (c.fact_id, c.document_text, c.measured_value, c.unit, c.resolution) == ("f_top", "90%", 0.45, "fraction",
                                                                                    "measured_data_wins")
    assert c.relative_difference == pytest.approx(1.0)
    assert "states 90%" in c.caveat() and "measured value is 45%" in c.caveat()


@pytest.mark.parametrize("text", [
    "The resolution rate for Network P1 incidents is 45%.",            # agrees
    "The resolution rate for Network P1 incidents is 44.9%.",          # within the tolerance
    "The resolution rate for Network rose from 30% to 45% this year.",  # one number in the sentence matches
    "The resolution rate for Storage is 90%.",                          # another subject
    "Network handles 90% of the backlog.",                              # another metric
])
def test_no_conflict_when_the_document_agrees_or_talks_about_something_else(text):
    assert fusion.conflicts(fusion.measured_from_facts(_facts()), [_doc(text)]) == []


def test_test_statistics_are_not_business_claims():
    assert [m.id for m in fusion.measured_from_facts(_facts())] == ["f_top", "f_base"]


def test_result_cells_as_measured_facts():
    m = fusion.measured_from_results([{"query_id": "q9", "columns": ["team", "resolution_rate", "tickets"],
                                       "rows": [["Network", 0.45, 120]]}])
    assert [(x.metric, x.unit, x.subject) for x in m] == [("resolution_rate", "fraction", "Network"), ("tickets", "value", "Network")]
    doc = _doc("Network resolution rate is 90%. Network had 120 tickets.")
    assert [c.fact_id for c in fusion.conflicts(m, [doc])] == ["q9:r0:resolution_rate"]


def test_fuse_keeps_the_two_kinds_apart():
    q = fusion.quantitative_from_facts(_facts(), [{"query_id": "q1", "result_hash": "rh1"}])
    cit = fusion.fuse("insight", "ins_1", text="Network resolves 45% of P1 incidents; policy says 90%.", quantitative=q,
                      documents=[_doc()], measured=fusion.measured_from_facts(_facts()), labels=["P1"])
    assert cit.summary() == {"quantitative": 1, "documents": 1, "conflicts": 1, "document_sourced_numbers": 1, "unbound_numbers": 0}
    again = Citations.model_validate(cit.model_dump(mode="json"))
    assert again == cit and isinstance(again.conflicts[0], Conflict)


# ------------------------------------------------------------------------------ persistence and Ask
@pytest.fixture
def db(sqlite_db):
    from analystos.db import models

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables["evidence_citation_set"]])
    return sqlite_db


def test_turn_citations_record_and_read_back(db, monkeypatch):
    from analystos.db.models import EvidenceCitationSet, LineageEdge, ModelCall, RunEvent
    from analystos.services import citations as svc

    monkeypatch.setattr(svc, "_sections", lambda session, receipts: {("kdoc_sla", "targets"): {"text": RULES_TEXT, "heading": "Targets"}})
    s = db()
    s.add(ModelCall(id=1, workspace_id="ws1", task_id="turn1", purpose="ask_sql", profile="llm_large", provider="openrouter", model="m", status="ok", context_receipts=[RECEIPT]))
    s.flush()
    turn = SimpleNamespace(id="turn1", workspace_id="ws1", question="resolution rate by team?", analysis=None,
                           explanation="Network resolves 45%; the SLA says 90%.",
                           result={"query_id": "q1", "result_hash": "rh1", "columns": ["team", "resolution_rate"],
                                   "rows": [["Network", 0.45], ["Database", 0.62]]})
    cit = svc.turn_citations(s, turn)
    assert [c.id for c in cit.quantitative] == ["q:q1"] and [d.path for d in cit.documents] == ["rules/sla.md"]
    assert {n.text: n.source for n in cit.narrative_numbers} == {"45%": "quantitative", "90%": "document"}
    assert len(cit.conflicts) == 1

    svc.record(s, cit, workspace_id="ws1", actor="user:u1")
    svc.record(s, cit, workspace_id="ws1", actor="user:u1")  # rewritten, not duplicated
    s.commit()
    row = s.query(EvidenceCitationSet).one()
    assert (row.subject_type, row.quantitative_count, row.document_count, row.conflict_count, row.document_ids) == \
        ("ask_turn", 1, 1, 1, ["kdoc_sla"])
    assert s.query(LineageEdge).filter_by(from_type="ask_turn", relation="cites", to_type="knowledge_document").count() == 1
    types = [e.type for e in s.query(RunEvent).all()]
    assert types.count("evidence.citations_recorded") == 2 and "evidence.conflict_flagged" in types
    view = svc.stored(s, "ask_turn", "turn1")
    assert view["recorded"] and view["summary"]["conflicts"] == 1
    assert [x["subject_id"] for x in svc.conflicts(s, "ws1")] == ["turn1"]


def test_unrecorded_subject_is_computed_and_marked(db, monkeypatch):
    from analystos.services import citations as svc

    monkeypatch.setattr(svc, "_sections", lambda session, receipts: {})
    turn = SimpleNamespace(id="turn2", workspace_id="ws1", question="q", analysis=None, explanation="", result=None)
    out = svc.for_turn(db(), turn)
    assert out["recorded"] is False and out["quantitative"] == [] and out["documents"] == []


def test_record_turn_quietly_never_fails_the_answer(monkeypatch):
    from analystos.services import citations as svc

    def boom(*_a, **_k):
        raise RuntimeError("index down")

    monkeypatch.setattr(svc, "turn_citations", boom)

    class Session:
        def begin_nested(self):
            from contextlib import nullcontext

            return nullcontext()

    svc.record_turn_quietly(Session(), SimpleNamespace(id="t", workspace_id="w"))  # logged, not raised


def test_rev_citations_failure_leaves_the_verdict_alone(monkeypatch):
    from analystos.agents import critic

    def boom(*_a, **_k):
        raise RuntimeError("no index")

    monkeypatch.setattr(critic, "insight_citations", boom)

    class Session:
        def begin_nested(self):
            from contextlib import nullcontext

            return nullcontext()

    assert critic._citations(Session(), SimpleNamespace(id="ins_1"), {}, "t", "f", None) is None


def test_event_types_and_migration():
    from analystos.contracts.events import EVENT_TYPES

    assert {"evidence.citations_recorded", "evidence.conflict_flagged"} <= EVENT_TYPES
    src = (Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0050_n8_evidence_citations.py").read_text()
    assert 'revision = "0050_n8"' in src and 'down_revision = "0045"' in src and '"evidence_citation_set"' in src
