"""P7-02 governed slice of the P4-V02 Ask benchmark (no services): the slice is well formed, every labelled
query is a valid SemanticQuery over the domain's approved metrics and columns, and the rules rung picks
exactly the labelled query for each plain-language governed question. The slice itself runs against the
stack (tests/integration/test_ask_benchmark.py)."""
from __future__ import annotations

import pytest
from evaluation import ask, ask_governed

from analystos.contracts.semantic import SemanticQuery
from analystos.semantic.compiler import match_question


@pytest.fixture(scope="module")
def spec():
    return ask_governed.load_governed()


def test_slice_is_well_formed(spec):
    assert ask_governed.problems(spec) == []
    assert set(spec) == set(ask.DOMAINS)
    assert sum(1 for d in spec.values() for q in d["questions"] if q.get("expect", "governed") == "governed") >= 15


@pytest.mark.parametrize("domain", ask.DOMAINS)
def test_labelled_queries_are_valid_and_the_rules_rung_picks_them(spec, domain):
    _, frame, qs = ask.frame_for(domain, 1)
    d = spec[domain]
    for m in d["metrics"]:
        assert set(m["dimensions"]) <= set(frame.columns), m["name"]
    fields = [{"name": f.name, "expressions": [{"expression": f.name}], "dimension": f.dimension}
              for f in ask_governed._fields(frame)]
    catalog = {"datasets": [{"name": domain, "source": "x.t", "fields": fields}], "synonyms": {},
               "metrics": {m["name"]: {"definition": {"name": m["name"], "dataset": domain, "display_name": m.get("display_name"),
                                                      "dimensions": m["dimensions"], "ai_context": m.get("ai_context"),
                                                      "expressions": [{"expression": m["expression"]}]}}
                           for m in d["metrics"]}}
    for q in d["questions"]:
        if q.get("expect", "governed") == "not_governed":
            assert match_question(q["q"], catalog) is None, q["id"]
            continue
        labelled = SemanticQuery.model_validate(q["query"])
        if "parameters" in q:
            assert SemanticQuery.model_validate(q["parameters"]["semantic_query"]) == labelled, q["id"]
        else:
            assert match_question(q["q"], catalog) == labelled, q["id"]
    assert qs.restricted_column not in {dim for m in d["metrics"] for dim in m["dimensions"]}
