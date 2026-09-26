"""Tests for `analystos.semantic.diff` (P7-02; ported from Atlas SM-7: reviewers see version deltas).

Pure unit tests -- no database required. Covers added/removed/changed field
classification, nested (per-metric) diffing, unchanged-fields-omitted, and
the "no published predecessor yet" edge case.

Origin: AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:tests/test_semantic_diff.py, unchanged
except the import; the AnalystOS snapshot tests are at the end.
"""

from __future__ import annotations

from analystos.semantic.diff import FieldDelta, diff_semantic_object, metric_snapshot, model_snapshot


class TestFlatFieldClassification:
    def test_identical_snapshots_produce_no_entries(self) -> None:
        snapshot = {"display_name": "Revenue", "definition": "Total revenue"}
        diff = diff_semantic_object(snapshot, snapshot)
        assert diff.entries == []
        assert not diff.has_changes

    def test_unchanged_fields_are_omitted_from_the_diff(self) -> None:
        before = {"display_name": "Revenue", "definition": "Total revenue"}
        after = {"display_name": "Revenue", "definition": "Total net revenue"}
        diff = diff_semantic_object(before, after)
        assert diff.changed_fields == ["definition"]

    def test_changed_field_reports_before_and_after(self) -> None:
        before = {"aggregation": "SUM"}
        after = {"aggregation": "AVG"}
        diff = diff_semantic_object(before, after)
        assert diff.entries == [
            FieldDelta(field="aggregation", change="changed", before="SUM", after="AVG")
        ]

    def test_added_field_has_no_before_value(self) -> None:
        before = {"display_name": "Revenue"}
        after = {"display_name": "Revenue", "owner_principal": "steward@bank.example"}
        diff = diff_semantic_object(before, after)
        assert diff.entries == [
            FieldDelta(
                field="owner_principal",
                change="added",
                before=None,
                after="steward@bank.example",
            )
        ]

    def test_removed_field_has_no_after_value(self) -> None:
        before = {"display_name": "Revenue", "owner_principal": "steward@bank.example"}
        after = {"display_name": "Revenue"}
        diff = diff_semantic_object(before, after)
        assert diff.entries == [
            FieldDelta(
                field="owner_principal",
                change="removed",
                before="steward@bank.example",
                after=None,
            )
        ]

    def test_a_field_explicitly_set_to_none_is_not_treated_as_absent(self) -> None:
        """`owner_principal: None` present in both snapshots is unchanged --
        distinct from the field being entirely absent from a dict.
        """
        before = {"owner_principal": None}
        after = {"owner_principal": None}
        diff = diff_semantic_object(before, after)
        assert diff.entries == []

    def test_list_valued_field_that_changes_is_reported_as_one_changed_entry(self) -> None:
        before = {"synonyms": ["revenue", "sales"]}
        after = {"synonyms": ["revenue", "sales", "net revenue"]}
        diff = diff_semantic_object(before, after)
        assert diff.entries == [
            FieldDelta(
                field="synonyms",
                change="changed",
                before=["revenue", "sales"],
                after=["revenue", "sales", "net revenue"],
            )
        ]

    def test_field_order_does_not_affect_equal_dicts(self) -> None:
        before = {"a": 1, "b": 2}
        after = {"b": 2, "a": 1}
        diff = diff_semantic_object(before, after)
        assert diff.entries == []

    def test_entries_are_sorted_by_field_name(self) -> None:
        before = {"z_field": "old", "a_field": "old"}
        after = {"z_field": "new", "a_field": "new"}
        diff = diff_semantic_object(before, after)
        assert [entry.field for entry in diff.entries] == ["a_field", "z_field"]


class TestNestedStructures:
    """A semantic model version bundles multiple metrics, keyed by slug so an
    added/removed/changed metric is its own entry rather than one opaque
    "metrics changed" blob.
    """

    def test_added_metric_is_one_added_entry_for_the_whole_nested_object(self) -> None:
        before = {"name": "Sales Model", "metrics": {}}
        after = {
            "name": "Sales Model",
            "metrics": {"revenue": {"aggregation": "SUM", "grain": "daily"}},
        }
        diff = diff_semantic_object(before, after)
        assert diff.entries == [
            FieldDelta(
                field="metrics.revenue",
                change="added",
                before=None,
                after={"aggregation": "SUM", "grain": "daily"},
            )
        ]

    def test_removed_metric_is_one_removed_entry(self) -> None:
        before = {"metrics": {"revenue": {"aggregation": "SUM"}}}
        after = {"metrics": {}}
        diff = diff_semantic_object(before, after)
        assert diff.entries == [
            FieldDelta(
                field="metrics.revenue",
                change="removed",
                before={"aggregation": "SUM"},
                after=None,
            )
        ]

    def test_changed_field_within_an_existing_metric_has_a_dotted_path(self) -> None:
        before = {"metrics": {"revenue": {"aggregation": "SUM", "grain": "daily"}}}
        after = {"metrics": {"revenue": {"aggregation": "AVG", "grain": "daily"}}}
        diff = diff_semantic_object(before, after)
        assert diff.entries == [
            FieldDelta(
                field="metrics.revenue.aggregation", change="changed", before="SUM", after="AVG"
            )
        ]

    def test_one_metric_changed_and_a_sibling_metric_untouched(self) -> None:
        before = {
            "metrics": {
                "revenue": {"aggregation": "SUM"},
                "cost": {"aggregation": "SUM"},
            }
        }
        after = {
            "metrics": {
                "revenue": {"aggregation": "AVG"},
                "cost": {"aggregation": "SUM"},
            }
        }
        diff = diff_semantic_object(before, after)
        assert diff.changed_fields == ["metrics.revenue.aggregation"]

    def test_multiple_metrics_added_removed_and_changed_in_one_diff(self) -> None:
        before = {
            "metrics": {
                "cost": {"aggregation": "SUM"},
                "revenue": {"aggregation": "SUM"},
            }
        }
        after = {
            "metrics": {
                "revenue": {"aggregation": "AVG"},
                "margin": {"aggregation": "SUM"},
            }
        }
        diff = diff_semantic_object(before, after)
        by_field = {entry.field: entry for entry in diff.entries}
        assert by_field["metrics.cost"].change == "removed"
        assert by_field["metrics.margin"].change == "added"
        assert by_field["metrics.revenue.aggregation"].change == "changed"
        assert len(diff.entries) == 3


class TestNoPublishedPredecessor:
    """A brand-new semantic object (first-ever submission) has no currently
    published version to diff against -- callers pass `{}` or `None` for
    `before` in that case, and every field on the proposed side is `added`.
    """

    def test_empty_before_reports_every_field_as_added(self) -> None:
        after = {"display_name": "Revenue", "definition": "Total revenue"}
        diff = diff_semantic_object({}, after)
        assert {entry.field for entry in diff.entries} == {"display_name", "definition"}
        assert all(entry.change == "added" for entry in diff.entries)

    def test_none_before_is_treated_the_same_as_empty_dict(self) -> None:
        after = {"display_name": "Revenue"}
        assert diff_semantic_object(None, after) == diff_semantic_object({}, after)

    def test_none_after_reports_every_field_as_removed(self) -> None:
        before = {"display_name": "Revenue"}
        diff = diff_semantic_object(before, None)
        assert diff.entries == [
            FieldDelta(field="display_name", change="removed", before="Revenue", after=None)
        ]

    def test_both_none_produces_no_entries(self) -> None:
        diff = diff_semantic_object(None, None)
        assert diff.entries == []


class TestIgnoreFields:
    def test_ignored_field_never_appears_even_if_it_differs(self) -> None:
        before = {"name": "Revenue", "created_by": "alice"}
        after = {"name": "Revenue", "created_by": "bob"}
        diff = diff_semantic_object(before, after, ignore_fields=frozenset({"created_by"}))
        assert diff.entries == []


class TestAnalystOSSnapshots:
    def test_a_metric_proposal_diff_names_the_expression_and_filter_changes(self) -> None:
        approved = {"name": "revenue", "expressions": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}],
                    "filters": ["paid = TRUE"], "dataset": "orders", "grain": None}
        proposed = {"name": "revenue", "expressions": [{"dialect": "ANSI_SQL", "expression": "SUM(net_amount)"}],
                    "filters": [], "dataset": "orders", "display_name": "Net revenue"}
        diff = diff_semantic_object(metric_snapshot(approved), metric_snapshot(proposed))
        by_field = {e.field: e for e in diff.entries}
        assert by_field["expression"] == FieldDelta("expression", "changed", "SUM(amount)", "SUM(net_amount)")
        assert by_field["filters"].change == "removed" and by_field["display_name"].change == "added"
        assert "dataset" not in by_field and "grain" not in by_field

    def test_model_structure_is_keyed_by_dataset_field_and_relationship(self) -> None:
        before = model_snapshot([{"name": "orders", "source": "sales.orders", "fields": [{"name": "amount", "expressions": [1]}]}],
                                [{"name": "o_c", "from": "orders", "to": "customers", "cardinality": None}])
        after = model_snapshot([{"name": "orders", "source": "sales.orders", "fields": []}],
                               [{"name": "o_c", "from": "orders", "to": "customers", "cardinality": "many_to_one"}])
        diff = diff_semantic_object(before, after)
        assert diff.changed_fields == ["datasets.orders.fields.amount", "relationships.o_c.cardinality"]
        assert diff.as_dict()["entries"][1]["after"] == "many_to_one"
