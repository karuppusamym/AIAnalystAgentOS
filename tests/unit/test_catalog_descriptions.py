"""Stream A pure rules: profile-aware rule descriptions, column enrichment payloads, shape masks, the shared profile
sanitizer, profile reuse, and when a discovered join may be validated without a person."""
from __future__ import annotations

from datetime import timedelta

from analystos.agents.metadata import auto_validates
from analystos.core.ids import utcnow
from analystos.db.models import SourceAsset
from analystos.services.crawler import profile_meta, profile_reusable
from analystos.skills import catalog as cat
from analystos.skills.profiling import column_is_sensitive, pattern_masks, sanitize_column_profile, shape_mask


def test_column_description_states_meaning_and_measured_facts():
    fk = cat.describe_column({"semantic_role": "foreign_key", "references_entity": "customer"},
                             references="customers.customer_id",
                             profile={"non_null": 90, "null_count": 10, "distinct": 12, "type_family": "numeric", "min": 1, "max": 12})
    assert fk == "Reference to customer (customers.customer_id); missing in 10% of rows; 12 distinct values."
    status = cat.describe_column({"semantic_role": "dimension"}, profile={
        "non_null": 50, "null_count": 0, "distinct": 3, "values_complete": True, "values": ["Open", "Closed", "Hold"]})
    assert status == "Descriptive attribute; always present; one of Open, Closed, Hold."
    ident = cat.describe_column({"semantic_role": "identifier"}, entity="order",
                                profile={"non_null": 50, "null_count": 0, "distinct": 50, "type_family": "numeric", "min": 1, "max": 50})
    assert ident == "Identifier of the order; always present; unique per row."
    when = cat.describe_column({"semantic_role": "timestamp"}, profile={
        "non_null": 5, "null_count": 0, "distinct": 5, "type_family": "datetime", "min": "2026-01-01T10:00", "max": "2026-03-01T09:00"})
    assert when == "Date and time; always present; from 2026-01-01 to 2026-03-01."
    flag = cat.describe_column({"semantic_role": "flag"}, profile={"non_null": 10, "null_count": 0, "distinct": 2, "true_count": 3})
    assert flag.endswith("true in 30% of non-empty rows.")
    assert cat.describe_column({"semantic_role": "measure", "unit": "count"}) == "Count."  # not profiled yet


def test_a_sensitive_column_description_states_no_values_or_ranges():
    prof = {"non_null": 50, "null_count": 0, "distinct": 3, "values_complete": True, "values": ["a@x", "b@x", "c@x"],
            "type_family": "text"}
    text = cat.describe_column({"semantic_role": "contact"}, profile=prof, sensitive=True)
    assert "a@x" not in text and text == "Contact detail; always present; 3 distinct values."
    salary = cat.describe_column({"semantic_role": "amount"}, sensitive=True,
                                 profile={"non_null": 5, "null_count": 0, "distinct": 5, "type_family": "numeric", "min": 1, "max": 9})
    assert salary == "Monetary amount; always present."


def test_table_description_is_one_or_two_plain_sentences():
    text = cat.describe_table({"role": "fact", "domain": "sales", "grain": "one row per order", "entity": "order"},
                              business_name="Orders", row_count=1204, key_columns=["id"], time_column="ordered_at",
                              time_range=("2025-01-01T00:00:00", "2025-06-30T00:00:00"), references=["customer", "region"])
    assert text == ("Fact table 'Orders' in the sales domain, one row per order. 1,204 rows; key id; dated by ordered_at "
                    "from 2025-01-01 to 2025-06-30; references customer, region.")
    assert cat.describe_table({"role": "unknown"}, business_name="X") == "Table 'X', one row per record."


def test_column_enrichment_sends_only_unsure_safe_columns_and_values_only_when_allowed():
    cols = [{"name": "u_flag2", "data_type": "varchar", "semantics": {"semantic_role": "dimension", "confidence": 0.35},
             "profile": {"null_rate": 0.1, "distinct_ratio": 0.01, "values_complete": True, "values": ["Y", "N"],
                         "patterns": [{"mask": "A", "share": 1.0}]}, "description_origin": "rule"},
            {"name": "status", "data_type": "varchar", "semantics": {"semantic_role": "dimension", "confidence": 0.7},
             "description_origin": "rule"},
            {"name": "attr1", "data_type": "varchar", "semantics": {"semantic_role": "dimension", "confidence": 0.35},
             "description_origin": "user"},
            {"name": "secret_code", "data_type": "varchar", "semantics": {"semantic_role": "code", "confidence": 0.3},
             "sensitive": True, "description_origin": None}]
    (only,) = cat.column_enrichment_payload(cols)
    assert only == {"name": "u_flag2", "type": "text", "rule_role": "dimension",
                    "shape": {"null_rate": 0.1, "distinct_ratio": 0.01, "patterns": ["A"]}}
    assert cat.column_enrichment_payload(cols, allow_values=True)[0]["values"] == ["Y", "N"]


def test_shape_masks_and_sanitizer():
    assert shape_mask("INC-00123") == "AAA-99999" and shape_mask("x" * 41) is None and shape_mask("é5") == "A9"
    assert pattern_masks(["AB-1", "CD-2", "E-33", None, ""]) == [{"mask": "AA-9", "share": 0.6667},
                                                                  {"mask": "A-99", "share": 0.3333}]
    prof = {"name": "email", "non_null": 5, "distinct": 5, "top_values": [{"value": "a@x"}], "values": ["a@x"],
            "patterns": [{"mask": "A@A"}], "avg_length": 9.0, "min": "a"}
    assert sanitize_column_profile(prof, sensitive=True) == {"name": "email", "non_null": 5, "distinct": 5}
    assert sanitize_column_profile(prof, sensitive=False) == prof
    assert column_is_sensitive([], {"pii": {"category": "email"}}) and not column_is_sensitive([], {"pii": {
        "category": "free_text_risk"}}) and column_is_sensitive(["restricted"], None)


def test_profile_reuse_needs_the_same_shape_load_and_age():
    a = SourceAsset(id="a", fingerprint="fp1", snapshot={"load_id": "load1", "sampling_method": "full"}, stats={})
    a.stats = {"profile_meta": profile_meta(a, {"row_count": 10})}
    assert a.stats["profile_meta"]["fingerprint"] == "fp1" and a.stats["profile_meta"]["snapshot_load"] == "load1"
    assert profile_reusable(a, 24) and not profile_reusable(a, 0)
    a.fingerprint = "fp2"
    assert not profile_reusable(a, 24)  # the table changed shape
    a.fingerprint, a.snapshot = "fp1", {"load_id": "load2"}
    assert not profile_reusable(a, 24)  # a new staged load
    a.snapshot = {"load_id": "load1"}
    a.stats["profile_meta"]["profiled_at"] = (utcnow() - timedelta(hours=30)).isoformat()
    assert not profile_reusable(a, 24)  # too old


def test_only_declared_or_corroborated_joins_validate_themselves():
    name_only = {"cardinality": "many_to_one", "confidence": 0.95, "evidence": {"source": "name_heuristic", "containment": 1.0},
                 "assessment": {"approvable": False}}
    assert not auto_validates(name_only)  # 0.95 confidence is not evidence
    assert auto_validates({**name_only, "assessment": {"approvable": True}})
    declared = {**name_only, "evidence": {"source": "declared", "containment": 1.0}}
    assert auto_validates(declared)
    assert not auto_validates({**declared, "evidence": {"source": "declared", "containment": 0.9}})  # orphans
    assert not auto_validates({**declared, "cardinality": "many_to_many"})


def test_a_nearly_unique_code_counts_its_repeats_and_future_dates_are_called_out():
    from analystos.skills.catalog import describe_column, describe_table

    number = describe_column({"semantic_role": "dimension"}, profile={
        "non_null": 20000, "null_count": 0, "distinct": 19985, "semantic_type": "id", "type_family": "text"})
    assert number == "Identifier-like code; always present; nearly unique: 15 values repeat (check for duplicates)."
    opened = describe_column({"semantic_role": "timestamp"}, profile={
        "non_null": 10, "null_count": 0, "distinct": 10, "type_family": "datetime",
        "min": "2025-09-01T10:00:00", "max": "2027-01-08T09:00:00"}, today="2026-09-27")
    assert opened.endswith("from 2025-09-01 to 2027-01-08; some rows are dated after today.")
    past = describe_column({"semantic_role": "timestamp"}, profile={
        "non_null": 10, "null_count": 0, "distinct": 10, "type_family": "datetime",
        "min": "2025-09-01", "max": "2026-08-31"}, today="2026-09-27")
    assert "after today" not in past
    api_table = describe_table({"role": "fact", "entity": "incident", "grain": "one row per incident"}, business_name="Incident",
                               kind="api", row_count=20000)
    assert "api" not in api_table.lower()
