"""Pure crawler rules: filtering, tag tightening, description precedence (no services)."""
from analystos.connectors.base import DiscoveredAsset
from analystos.services.crawler import _description_writable, crawler_tags, filter_assets, in_crawl_scope
from analystos.skills.catalog import PiiResult


def _a(schema: str, name: str) -> DiscoveredAsset:
    return DiscoveredAsset(source_name=name, name=name, schema_name=schema)


def test_filter_include_exclude_and_stable_cap():
    assets = [_a("sales", "orders"), _a("sales", "tmp_orders"), _a("hr", "employee"), _a("sales", "region")]
    kept, cut = filter_assets(assets, ["sales.*"], ["tmp_*"], max_tables=10)
    assert [a.name for a in kept] == ["orders", "region"] and cut == 0
    kept, cut = filter_assets(assets, None, None, max_tables=2)
    assert [f"{a.schema_name}.{a.name}" for a in kept] == ["hr.employee", "sales.orders"] and cut == 2


def test_crawl_scope_matches_key_or_bare_name():
    assert in_crawl_scope("sales.region", None, None)
    assert not in_crawl_scope("sales.region", None, ["region"])
    assert not in_crawl_scope("sales.region", ["hr.*"], None)
    assert in_crawl_scope("sales.region", ["SALES.*"], ["tmp_*"])


def test_tags_only_tighten():
    assert crawler_tags(["restricted"], PiiResult()) == ["restricted"]
    assert crawler_tags([], PiiResult(category="email", sensitivity="confidential", confidence=0.9)) == ["pii"]
    assert crawler_tags([], PiiResult(category="credential", sensitivity="restricted", confidence=0.9)) == ["pii", "restricted"]
    # weak or free-text signals never tag (they would silently remove columns from analysis)
    assert crawler_tags([], PiiResult(category="person_name", sensitivity="confidential", confidence=0.5)) == []
    assert crawler_tags([], PiiResult(category="free_text_risk", sensitivity="internal", confidence=0.9)) == []


def test_description_precedence():
    assert _description_writable(None, False, None, "orders")
    assert _description_writable("rule", False, "Fact table of orders.", "orders")
    assert _description_writable(None, False, "TBD", "orders")
    for origin in ("user", "source", "model"):
        assert not _description_writable(origin, False, "x", "orders")
    assert not _description_writable("rule", True, "x", "orders")  # reviewed


def test_owner_tags_keep_columns_out_of_enrichment_payloads():
    from analystos.skills.catalog import enrichment_batches

    item = {"key": "s.t", "name": "t", "semantics": {"business_name": "T", "role": "unknown", "confidence": 0.3},
            "columns": [{"name": "diagnosis_code", "data_type": "text", "sensitivity": "restricted"},
                        {"name": "visit_count", "data_type": "integer"}]}
    [[payload]] = enrichment_batches([item])
    assert [c["name"] for c in payload["columns"]] == ["visit_count"] and payload["columns_omitted"] == 1


def test_stale_crawl_detection_uses_last_logged_progress():
    from datetime import timedelta
    from types import SimpleNamespace

    from analystos.core.ids import utcnow
    from analystos.services.crawler import STALE_SECONDS, _last_activity

    old = utcnow() - timedelta(seconds=STALE_SECONDS * 3)
    dead = SimpleNamespace(started_at=old, log=[{"at": (old + timedelta(seconds=5)).isoformat()}])
    alive = SimpleNamespace(started_at=old, log=[{"at": utcnow().isoformat()}, {"bad": 1}])
    assert _last_activity(dead) > STALE_SECONDS and _last_activity(alive) < 60
