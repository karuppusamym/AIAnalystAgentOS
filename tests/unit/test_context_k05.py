"""P4-K05 context compiler over the pack: fused ranking, one-hop gating, section-level excerpts
with receipts, and memory/external sections that cannot crowd out glossary terms."""
from analystos.context.compiler import (
    NO_MATCH,
    KnowledgeItem,
    compile_context,
    external_items,
    focused_excerpt,
    rank_items,
    terms,
)
from analystos.contracts.platform import PurposeProfile

OBJECTIVE = "What drives SLA breaches for priority 1 incidents?"


def glossary_term() -> KnowledgeItem:
    return KnowledgeItem(id="ctx_sla", section="glossary", name="SLA breach",
                         text="An incident breaches its SLA when made_sla is false.", source="pack:itsm")


def episodes(n: int = 30) -> list[KnowledgeItem]:
    return [KnowledgeItem(id=f"ep{i}", section="episodes", name=f"Run {i}: SLA breaches for priority 1 incidents",
                          text="SLA breaches priority incidents drivers found. " * 20, source="agent") for i in range(n)]


def test_episodes_cannot_crowd_out_glossary_terms():
    # episodes listed FIRST, maximally relevant and plentiful; the budget is tight
    profile = PurposeProfile(sections=["episodes", "glossary"], max_items_per_section=30, item_chars=160, max_chars=4_000)
    c = compile_context("planning", profile, objective=OBJECTIVE, required={"objective": OBJECTIVE},
                        knowledge=[*episodes(), glossary_term()])
    assert [i["id"] for i in c.body["glossary"]] == ["ctx_sla"]
    sent = c.body["episodes"]
    assert isinstance(sent, list) and 0 < len(sent) < 30
    share = len(json_compact(sent))
    assert share <= int(profile.max_chars * profile.supplementary_share) + 50
    assert {o["reason"] for o in c.omitted if o["section"] == "episodes"} >= {"section share"}
    assert list(c.body)[:3] == ["objective", "episodes", "glossary"]  # prompt keeps the profile order
    assert c.chars <= profile.max_chars


def test_memory_is_given_back_before_glossary_when_the_budget_is_short():
    many = [KnowledgeItem(id=f"g{i}", section="glossary", name=f"SLA breach priority term {i}",
                          text="SLA breach priority incidents. " * 12, source="user") for i in range(12)]
    profile = PurposeProfile(sections=["glossary", "prior_findings", "episodes"], max_items_per_section=12, max_chars=2_600,
                             supplementary_share=0.5)
    prior = [KnowledgeItem(id="ins1", section="prior_findings", name="Priority 1 SLA breaches", text="P1 breach SLA more.",
                           source="run:old")]
    c = compile_context("hypothesis_generation", profile, objective=OBJECTIVE, required={"objective": OBJECTIVE},
                        knowledge=[*many, *prior, *episodes(3)])
    assert isinstance(c.body["glossary"], list) and c.body["glossary"]
    dropped_glossary = [o for o in c.omitted if o["section"] == "glossary"]
    kept_memory = [i for s in ("prior_findings", "episodes") if isinstance(c.body[s], list) for i in c.body[s]]
    # glossary items are only ever dropped when no memory item is left to give back
    assert not (dropped_glossary and kept_memory)


def json_compact(value) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


def test_rank_items_fuses_overlap_and_index_rank_and_gates_one_hop():
    q = terms(OBJECTIVE)
    a = KnowledgeItem(id="d1#definition", section="glossary", name="SLA breach", text="made_sla false", source="okf:w",
                      path="glossary/sla.md", retrieval_rank=2, lexical_share=0.5)
    b = KnowledgeItem(id="d2#definition", section="glossary", name="Service level agreement",
                      text="The contracted time to resolve.", source="okf:w", path="glossary/agreement.md", retrieval_rank=1,
                      lexical_share=0.25)  # no overlap with the question's words, but the index ranks it first
    hop = KnowledgeItem(id="d3#definition", section="business_rules", name="Escalation", text="escalate after 30 min",
                        source="okf:w", path="rules/escalation.md", retrieval_rank=5, via="glossary/sla.md")
    orphan = KnowledgeItem(id="d4#definition", section="business_rules", name="Holidays", text="calendar",
                           source="okf:w", path="rules/holidays.md", retrieval_rank=6, via="glossary/other.md")
    ranked = rank_items([a, b, hop, orphan], q, set(), 0.15)
    by_id = {r.item.id: r for r in ranked}
    assert ranked[0].item.id == "d1#definition"  # overlap rank 1 + index rank 2 beats index rank 1 alone
    assert not by_id["d2#definition"].passed  # the index orders candidates, it does not admit them
    assert by_id["d3#definition"].passed  # one hop from a passing section
    # among passing items the index rank decides: equal overlap, the better index rank first
    c = KnowledgeItem(id="d5#definition", section="glossary", name="Breach of SLA", text="x", source="okf:w",
                      path="glossary/breach.md", retrieval_rank=1)
    d = KnowledgeItem(id="d6#definition", section="glossary", name="Breach SLA", text="x", source="okf:w",
                      path="glossary/breach2.md", retrieval_rank=9)
    order = [r.item.id for r in rank_items([d, c], q, set(), 0.15)]
    assert order == ["d5#definition", "d6#definition"]
    assert not by_id["d4#definition"].passed  # one hop from nothing that passed


def test_focused_excerpt_keeps_the_sentences_about_the_question():
    text = ("Incidents are logged by the service desk. Opening hours are 8 to 18. The weather does not matter here. "
            "An SLA breach happens when the resolution time exceeds the priority target. Other paragraphs follow here.")
    out = focused_excerpt(text, terms("SLA breach priority"), 120)
    assert "SLA breach happens" in out and "weather" not in out and out.startswith("…")
    assert focused_excerpt("short text", terms("x"), 50) == "short text"


def test_section_items_carry_path_anchor_and_hashes_in_receipts():
    item = KnowledgeItem(id="kdoc_x#definition", section="glossary", name="SLA breach", text="An SLA breach is late.",
                         source="okf:workspace", document_id="kdoc_x", path="glossary/sla.md", anchor="definition",
                         sha256="a" * 64, section_sha256="b" * 64, retrieval_rank=1, lexical_share=1.0)
    c = compile_context("planning", PurposeProfile(sections=["glossary"]), objective="SLA breach", required={"q": 1},
                        knowledge=[item])
    r = next(r for r in c.receipts if r["section"] == "glossary")
    assert (r["path"], r["anchor"], r["sha256"], r["section_sha256"], r["document_id"]) == \
        ("glossary/sla.md", "definition", "a" * 64, "b" * 64, "kdoc_x")


def test_external_provider_results_are_their_own_untrusted_section():
    results = [{"provider": "mcp", "status": "MATCHED", "items": [
        {"title": "Customer", "heading": "Definition", "text": "A customer is an account with an SLA contract.",
         "path": "bundle/concepts/customer.md", "anchor": "definition", "sha256": "c" * 64}]},
        {"provider": "okf_import", "status": "UNAVAILABLE", "items": []}]
    items = external_items(results)
    assert [(i.section, i.trusted, i.source) for i in items] == [("external", False, "mcp:bundle/concepts/customer.md")]
    profile = PurposeProfile(sections=["glossary", "external"])
    c = compile_context("planning", profile, objective="SLA contract customer", required={"q": 1},
                        knowledge=[*items, glossary_term()])
    assert c.body["external"][0]["trusted"] is False and c.body["external"][0]["name"] == "Customer"
    assert any(r["section"] == "external" and r["path"] == "bundle/concepts/customer.md" for r in c.receipts)
    none = compile_context("planning", profile, objective="SLA", required={"q": 1}, knowledge=[glossary_term()])
    assert none.body["external"] == NO_MATCH
