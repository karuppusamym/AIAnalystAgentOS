"""Second context review (2026-09-28, the Operations Intelligence Demo export): a domain pack's term that is a
condition on a column is not that column's meaning, connector text that only restates a name is no description,
unmeasured columns say "not profiled", and catalog documents are titled by business name."""
from __future__ import annotations

from analystos.context.export import profile_line
from analystos.knowledge import crawl_docs
from analystos.skills import catalog as cat

PACK_TERMS = [
    {"id": "t_sla", "name": "SLA breach", "synonyms": ["missed SLA"], "mapped_columns": ["incident.made_sla"], "mapped_identity": False},
    {"id": "t_ah", "name": "After-hours incident", "synonyms": [], "mapped_columns": ["incident.opened_at"], "mapped_identity": False},
    {"id": "t_grp", "name": "Assignment group", "synonyms": [], "mapped_columns": ["incident.assignment_group"], "mapped_identity": False},
    {"id": "t_p1", "name": "Priority 1 (P1)", "synonyms": ["P1"], "mapped_columns": ["incident.priority"], "mapped_identity": False},
]


def _links(terms, *names):
    cols = [{"fq": f"src.incident.{n}", "name": n, "business_name": None} for n in names]
    return {(k.column_fq.rsplit(".", 1)[-1], k.relation): k.term_id for k in cat.link_glossary(cols, terms)}


def test_a_pack_term_that_is_a_condition_on_a_column_is_related_not_its_meaning():
    links = _links(PACK_TERMS, "made_sla", "opened_at", "assignment_group", "priority")
    assert links[("made_sla", "related")] == "t_sla" and ("made_sla", "is") not in links
    assert links[("opened_at", "related")] == "t_ah" and ("opened_at", "is") not in links
    assert links[("priority", "related")] == "t_p1" and ("priority", "is") not in links
    assert links[("assignment_group", "is")] == "t_grp"


def test_a_reviewed_workspace_term_mapping_is_the_columns_meaning():
    terms = [{**PACK_TERMS[0], "mapped_identity": True}]
    assert _links(terms, "made_sla") == {("made_sla", "is"): "t_sla"}


def test_connector_text_that_restates_the_name_is_no_description():
    assert cat.restates_name("ServiceNow table incident (Incident)", "incident", "Incident")
    assert cat.restates_name("Uploaded file orders.csv", "orders")
    assert cat.restates_name("From column 'Order Date' of orders.csv", "order_date", "Order Date", "orders")
    assert not cat.restates_name("One row per case of the event log", "cases")
    assert not cat.restates_name("Display value of assignment_group", "assignment_group_name")


def test_value_patterns_alone_are_not_a_profile():
    assert profile_line({"patterns": [{"mask": "AAA9999999", "share": 1.0}]}) == "not profiled"
    assert profile_line({"null_rate": 0.0, "distinct": 3}).startswith("nulls 0.0%")


def test_catalog_documents_are_titled_by_business_name():
    docs = crawl_docs.table_documents({"schema_name": "src_src_1", "name": "cmdb_ci", "id": "a1",
                                       "business_name": "Configuration Item"}, [{"name": "name"}],
                                      source_id="src_1", source_name="ServiceNow")
    assert "title: Configuration Item (cmdb_ci)" in docs[crawl_docs.table_path("src_src_1.cmdb_ci")]
    same = crawl_docs.table_documents({"schema_name": "s", "name": "orders", "id": "a2", "business_name": "Orders"},
                                      [{"name": "id"}], source_id="src_1", source_name="Shop")
    assert "title: Orders\n" in same[crawl_docs.table_path("s.orders")]
