"""Stream D: the context export's pure renderers (deterministic, OKF-conformant, no values a policy keeps inside),
the preview's purposes, and the per-workspace view of the shared context cache."""
from __future__ import annotations

import io
import json
import zipfile

from analystos.context import export
from analystos.knowledge import okf
from analystos.skills.profiling import SENSITIVE_PROFILE_FIELDS

NUMERIC = {"name": "amount", "data_type": "numeric", "type_family": "numeric", "semantic_type": "numeric", "non_null": 10,
           "null_count": 0, "null_rate": 0.0, "distinct": 10, "min": 1.5, "max": 99.0, "mean": 50.0,
           "histogram": [{"bin": 0, "count": 3}]}
CATEGORY = {"name": "segment", "data_type": "text", "type_family": "text", "semantic_type": "categorical", "non_null": 10,
            "null_rate": 0.0, "distinct": 2, "min": "Enterprise", "max": "SMB", "values_complete": True,
            "values": ["Enterprise", "SMB"], "top_values": [{"value": "SMB", "count": 6}]}
EMAIL = {"name": "email", "data_type": "text", "type_family": "text", "semantic_type": "text", "non_null": 10, "null_rate": 0.0,
         "distinct": 10, "min": "a@x.io", "max": "z@x.io", "top_values": [{"value": "a@x.io", "count": 1}], "avg_length": 9.0}


def _column(name: str, profile: dict, tags: list[str] | None = None) -> dict:
    return {"name": name, "data_type": profile["data_type"], "semantic_type": profile["semantic_type"], "role": None, "unit": None,
            "is_key": False, "nullable": True, "business_name": name.title(), "business_name_origin": "rule",
            "description": f"The {name}.", "description_origin": "rule", "reviewed": False, "tags": tags or [], "pii": None,
            "glossary": None, "sensitive": bool(tags),
            "profile": export.export_profile(profile, tags, None, samples_allowed=False)}


def _content() -> dict:
    return export._clean({
        "workspace": {"id": "ws1", "name": "Shop | analytics", "slug": "shop-analytics", "objective": "Grow revenue",
                      "description": "Orders and customers"},
        "scope": {"kind": "workspace"},
        "policy": {"data_samples_included": False, "never_included": list(export.NEVER_INCLUDED)},
        "brief": {"version": 2, "facts": [{"key": "data_semantics.grain:shop.orders", "group": "data_semantics", "field": "grain",
                                           "subject": "shop.orders", "value": "one row per order", "origin": "user",
                                           "review_state": "reviewed", "confidence": None, "note": None}],
                  "open_questions": [{"key": "time_measures.unit:shop.orders.amount", "group": "time_measures", "field": "unit",
                                      "subject": "shop.orders.amount", "value": "USD", "origin": "rule",
                                      "review_state": "suggested", "confidence": 0.6, "note": None}], "rejected": 0},
        "sources": [{"id": "src_shop", "name": "shop", "kind": "csv", "execution_mode": "staged", "status": "ready",
                     "last_discovered_at": None, "has_error": False, "assets": 2, "selected_assets": 2,
                     "last_crawl": {"id": "cr1", "mode": "full", "trigger": "manual", "status": "succeeded",
                                    "started_at": "2026-09-27T10:00:00", "finished_at": "2026-09-27T10:01:00",
                                    "drift": {"added_columns": ["shop.orders.channel"]}}}],
        "assets": [
            {"id": "a1", "fq": "shop.orders", "source_id": "src_shop", "source": "shop", "schema_name": "shop", "name": "orders",
             "kind": "table", "selected": True, "role": "fact", "domain": "sales", "grain": "one row per order", "entity": "order",
             "confidence": 0.8, "row_count": 60, "key": {"columns": ["order_id"], "evidence": "declared", "unique": True},
             "time_column": "ordered_at", "business_name": "Orders", "business_name_origin": "rule", "description": "Customer orders.",
             "description_origin": "rule", "reviewed": True, "last_crawled_at": None, "profile_meta": {"rows_profiled": 60},
             "columns": [_column("amount", NUMERIC), _column("segment", CATEGORY)]},
            {"id": "a2", "fq": "shop.customers", "source_id": "src_shop", "source": "shop", "schema_name": "shop",
             "name": "customers", "kind": "table", "selected": True, "role": "dimension", "domain": "sales", "grain": None,
             "entity": "customer", "confidence": 0.7, "row_count": 10, "key": {"columns": [], "evidence": "none", "unique": None},
             "time_column": None, "business_name": None, "business_name_origin": None, "description": None,
             "description_origin": None, "reviewed": False, "last_crawled_at": None, "profile_meta": None,
             "columns": [_column("email", EMAIL, ["pii"])]}],
        "relationships": {"legacy": [{"id": "r1", "from": "shop.orders.customer_id", "to": "shop.customers.customer_id",
                                      "cardinality": "many_to_one", "confidence": 0.9, "validated": True, "origin": "discovered",
                                      "evidence": {"containment": 1.0}}],
                          "candidates": [{"id": "c1", "status": "pending", "from": "shop.orders", "from_columns": ["customer_id"],
                                          "to": "shop.customers", "to_columns": ["customer_id"], "cardinality": "many_to_one",
                                          "containment": 1.0, "confidence": 0.9, "origin": "crawler", "measured_at": None,
                                          "evidence": {"rows": 60}, "name": None}]},
        "semantic_models": [{"version": 1, "name": "shop", "status": "approved", "description": "The shop model", "origin": "user",
                             "created_at": None, "content_hash": "h", "ai_context": None,
                             "datasets": [{"name": "orders", "source": "shop.orders", "primary_key": ["order_id"], "fields": []}],
                             "relationships": []}],
        "metrics": [{"name": "revenue", "display_name": "Revenue", "version": 3, "status": "approved", "expression": "SUM(amount)",
                     "dialect": "postgres", "description": "Total order amount.", "dataset": "orders", "grain": None,
                     "filters": [], "dimensions": [], "source_columns": ["shop.orders.amount"], "format": None,
                     "owner": "Ana", "approved_at": None}],
        "glossary": [{"id": "e1", "kind": "term", "name": "Revenue", "body": "Money from orders.", "synonyms": ["sales"],
                      "mapped_columns": ["shop.orders.amount"], "origin": "user", "trusted": True},
                     {"id": "e2", "kind": "rule", "name": "Refunds", "body": "Refunds are negative amounts.", "synonyms": [],
                      "mapped_columns": [], "origin": "user", "trusted": False}],
        "knowledge_documents": [{"pack": "workspace", "pack_kind": "workspace", "path": "glossary/sla.md", "title": "SLA",
                                 "type": "Glossary Term", "kind": "term", "status": "stable", "trust_tier": "unverified",
                                 "revision": 1}],
        "analysis_contexts": [{"key": "weekly", "version": 1, "status": "published", "title": "Weekly revenue",
                               "purpose": "Explain weekly revenue", "business_description": "", "question_template": "Why did revenue move?",
                               "sources": ["shop"], "metric_names": ["revenue"], "published_at": None}],
        "suggested_model": {"available": True, "tables": [{"asset_id": "a1", "fq": "shop.orders", "name": "orders", "role": "fact",
                                                            "entity": "order", "grain": None, "business_name": None,
                                                            "primary_key": {"columns": ["order_id"], "evidence": "declared", "unique": True},
                                                            "time_column": "ordered_at", "measures": ["amount"], "dimensions": [],
                                                            "confidence": 0.8, "issues": []}],
                            "relationships": [], "metrics": [{"name": "sum_amount", "label": "Total Amount", "expression": "SUM(amount)",
                                                              "table_fq": "shop.orders", "reason": "amount column"}],
                            "star_schemas": [], "issues": [], "summary": {"tables": 1, "relationships": 0, "issues": 0}},
        "counts": {"assets": 2},
    })


def test_profiles_keep_only_what_the_policy_allows():
    sensitive = export.export_profile(EMAIL, ["pii"], None, samples_allowed=True)
    assert set(sensitive) <= SENSITIVE_PROFILE_FIELDS and sensitive["distinct"] == 10  # completeness + cardinality only
    pii_by_semantics = export.export_profile(EMAIL, [], {"pii": {"category": "email"}}, samples_allowed=True)
    assert set(pii_by_semantics) <= SENSITIVE_PROFILE_FIELDS
    no_samples = export.export_profile(CATEGORY, [], None, samples_allowed=False)
    assert not {"values", "top_values", "values_complete", "min", "max"} & set(no_samples) and no_samples["distinct"] == 2
    numeric = export.export_profile(NUMERIC, [], None, samples_allowed=False)
    assert (numeric["min"], numeric["max"], numeric["histogram"]) == (1.5, 99.0, [{"bin": 0, "count": 3}])
    samples = export.export_profile(CATEGORY, [], None, samples_allowed=True)
    assert samples["values"] == ["Enterprise", "SMB"]
    assert export.export_profile(None, [], None, samples_allowed=False) is None


def test_json_is_versioned_and_its_digest_ignores_the_export_time():
    content = _content()
    a = json.loads(export.render_json(content, generated_at="2026-09-27T10:00:00"))
    b = json.loads(export.render_json(content, generated_at="2026-09-28T11:00:00"))
    assert a["format"] == "analystos.context/v1" and a["content_digest"] == b["content_digest"] == export.digest(content)
    assert a["assets"][0]["fq"] == "shop.orders" and a["generated_at"] != b["generated_at"]
    changed = {**content, "glossary": content["glossary"][:1]}
    assert export.digest(changed) != a["content_digest"]


def test_markdown_is_readable_and_escapes_table_cells():
    text = export.render_markdown(_content(), generated_at="2026-09-27T10:00:00")
    assert text.startswith("# Shop | analytics — data context\n")
    for heading in ("## Brief (version 2)", "### Open questions", "## Sources", "### shop.orders — Orders", "## Relationships",
                    "## Semantic model", "## Approved metrics", "## Glossary, definitions and rules", "## Knowledge documents",
                    "## Analysis contexts", "## Suggested data model"):
        assert heading in text, heading
    assert "| amount | numeric |" in text and "range 1.5 .. 99.0" in text
    assert "Enterprise" not in text and "a@x.io" not in text  # no value lists without the samples policy
    assert "added columns 1" in text and "SUM(amount)" in text


def test_okf_bundle_is_deterministic_conformant_and_linked():
    content = _content()
    files = export.render_okf(content, generated_at="2026-09-27T10:00:00")
    assert files == export.render_okf(content, generated_at="2026-09-27T10:00:00")
    assert okf.check_publish_policy(files) == []  # conformant, safe paths, no dangling links
    assert {"index.md", "brief.md", "sources/src_shop.md", "tables/shop.orders.md", "tables/shop.customers.md",
            "glossary/term-revenue.md", "glossary/rule-refunds.md", "metrics/revenue-v3.md", "model/semantic-model-v1.md",
            "model/relationships.md", "model/suggested.md", "contexts/weekly-v1.md"} <= set(files)
    index = files["index.md"].decode()
    assert index.startswith("---\nokf_version: '0.2'\n---\n") and "(tables/shop.orders.md)" in index
    metric = okf.parse_document("metrics/revenue-v3.md", files["metrics/revenue-v3.md"])
    assert metric.type == "Metric" and "SUM(amount)" in metric.body and metric.extension["mapped_columns"] == ["shop.orders.amount"]
    rule = okf.parse_document("glossary/rule-refunds.md", files["glossary/rule-refunds.md"])
    assert rule.type == "Business Rule" and rule.status == "draft"
    archive = export.render(content, "okf", generated_at="2026-09-27T10:00:00")
    assert archive == export.render(content, "okf", generated_at="2026-09-27T10:00:00")
    assert sorted(zipfile.ZipFile(io.BytesIO(archive)).namelist()) == sorted(files)


def test_filenames_name_the_workspace_scope_and_day():
    content = _content()
    assert export.filename(content, "okf", "2026-09-27") == "shop-analytics-context-2026-09-27.okf.zip"
    scoped = {**content, "scope": {"kind": "source", "source_id": "src_hr", "source_name": "HR system"}}
    assert export.filename(scoped, "json", "2026-09-27") == "shop-analytics-hr-system-context-2026-09-27.json"
    assert export.filename(content, "markdown", "2026-09-27").endswith(".md")


def test_source_filter_keeps_what_references_its_tables():
    class Assertion:
        def __init__(self, key, subject, state):
            self.key, self.subject, self.review_state = key, subject, state
            self.group, self.field, self.value, self.origin, self.confidence, self.note = "domain", "alias", "x", "rule", None, None

        @property
        def effective(self):
            return self.review_state in ("reviewed", "validated")

    rows = [Assertion("a", "shop.orders.amount", "reviewed"), Assertion("b", "hr.people", "suggested"),
            Assertion("c", None, "reviewed"), Assertion("d", "shop.orders", "suggested")]
    brief = export._brief(None, rows, {"shop.orders"})
    assert [a["key"] for a in brief["facts"]] == ["a"] and [a["key"] for a in brief["open_questions"]] == ["d"]
    assert [a["key"] for a in export._brief(None, rows, None)["facts"]] == ["a", "c"]

    class Suggest:
        @staticmethod
        def suggest(session, workspace_id):
            t = lambda i, fq: {"asset_id": i, "fq": fq}  # noqa: E731
            return {"tables": [t("a1", "shop.orders"), t("h1", "hr.people")],
                    "relationships": [{"from": {"asset_id": "h1"}, "to": {"asset_id": "h1"}, "cardinality": "one_to_one"}],
                    "metrics": [{"table_fq": "shop.orders"}, {"table_fq": "hr.people"}], "star_schemas": [{"fact": "a1"}],
                    "issues": [{"asset_id": "h1", "code": "orphan_table"}]}

    s = export._suggested(None, "ws1", Suggest, {"a1"})
    assert [t["fq"] for t in s["tables"]] == ["shop.orders"] and s["relationships"] == [] and len(s["metrics"]) == 1
    assert s["issues"] == [] and s["star_schemas"] == [{"fact": "a1"}]
    assert export._in_tables("shop.orders.amount", {"shop.orders"}) and not export._in_tables("shop.ordersx.a", {"shop.orders"})

    class Broken:
        @staticmethod
        def suggest(session, workspace_id):
            raise KeyError("x")

    assert export._suggested(None, "ws1", Broken, None) == {"available": False, "reason": "KeyError"}


def test_preview_purposes_have_profiles_prompts_and_agents():
    from analystos.agents.prompts import PROMPTS
    from analystos.context import preview
    from analystos.contracts.platform import PlatformSettings

    profiles = PlatformSettings().context.profiles
    for purpose, spec in preview.PURPOSES.items():
        assert purpose in profiles and spec.prompt in PROMPTS and spec.label, purpose
    assert [p["purpose"] for p in preview.purposes()][0] == "sql_generation"
    text = preview.as_text({"label": "Writing SQL for Ask", "purpose": "sql_generation", "question": "q",
                            "scope": {"assets": ["shop.orders"]}, "knowledge_version": "kv1",
                            "estimated_tokens": {"stable": 10, "volatile": 2, "total": 12},
                            "cache": {"kind": "retrieval", "hit": True, "shared": False},
                            "sections": [{"name": "catalog", "chars": 40}], "system_text": "SYS",
                            "preamble_text": "PRE", "volatile_text": "{\"question\":\"q\"}", "refused": None})
    assert text.index("SYS") < text.index("PRE") < text.index('{"question":"q"}') and "10 stable" in text


def test_workspace_cache_view_counts_and_clears_only_that_workspace():
    from analystos.agents import common
    from analystos.context import cache as context_cache
    from analystos.context.compiler import CompiledContext

    context_cache.put(context_cache.key("retrieval", "ws1", {"q": 1}), [1], chars=10)
    context_cache.put(context_cache.key("retrieval", "ws2", {"q": 1}), [1], chars=10)
    common._COMPILED.put("k", "sql_generation", CompiledContext(purpose="sql_generation", header={}, body={}), workspace_id="ws1")
    assert common._COMPILED.get("k", "sql_generation", workspace_id="ws1") is not None
    assert common._COMPILED.get("k", "sql_generation", workspace_id="ws2") is None
    assert context_cache.get("retrieval", "sql_generation", context_cache.key("retrieval", "ws1", {"q": 1})) == [1]
    with context_cache.unrecorded():  # an inspection is not a hit
        assert context_cache.get("retrieval", "sql_generation", context_cache.key("retrieval", "ws1", {"q": 1})) == [1]
    assert context_cache.peek(context_cache.key("retrieval", "ws1", {"q": 1}))
    assert not context_cache.peek(context_cache.key("retrieval", "ws1", {"q": 2})) and not context_cache.peek(None)

    view = context_cache.workspace_stats("ws1")
    assert view["entries"] == {"compiled": 1, "retrieval": 1} and view["total_entries"] == 2
    assert view["by_kind"]["retrieval"]["sql_generation"] == {"hits": 1, "misses": 0, "chars_reused": 10}
    assert view["by_kind"]["compiled"]["sql_generation"]["hits"] == 1 and view["totals"]["hits"] == 2
    assert context_cache.stats()["retrieval"]["sql_generation"]["hits"] == 1  # the platform view counts it too
    assert context_cache.clear_workspace("ws1") == 2
    assert context_cache.workspace_stats("ws1")["total_entries"] == 0 and context_cache.workspace_stats("ws1")["by_kind"] == {}
    assert context_cache.workspace_stats("ws2")["total_entries"] == 1
    assert context_cache.workspace_of(context_cache.key("retrieval", "ws2", {})) == "ws2"
    assert context_cache.workspace_of(context_cache.key("retrieval", None, {})) is None


def test_a_sensitive_columns_values_are_reported_as_withheld_and_a_query_dataset_is_one_short_line():
    assert export.profile_line({"distinct": 4}, withheld=True) == "distinct 4; values withheld (sensitive)"
    assert export.profile_line({"distinct": 4}) == "distinct 4"
    line = export._dataset_source("SELECT a,\n   b FROM   src.some_table WHERE x = 1 " * 5)
    assert "\n" not in line and len(line) <= 80 and line.endswith("...")
    assert export._dataset_source("src.orders") == "src.orders"


def test_a_polymorphic_reference_names_the_tables_its_values_live_in():
    plain = {"role": "foreign_key"}
    poly = {"role": "foreign_key", "polymorphic_reference": {"targets": [{"asset": "sn.incident"}, {"asset": "sn.sc_task"}]}}
    assert export._role_text(plain) == "foreign_key" and export._role_text({}) == ""
    assert export._role_text(poly) == "foreign_key → one of sn.incident, sn.sc_task"
