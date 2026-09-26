"""P7-09: Atlas's relationship assessment rules as the spec, on measured facts.

Ported from AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:tests/test_relationship_validation.py
("Assessment" section). Same cases and conclusions; adapted to AnalystOS's vocabulary (lower-case
cardinality) and to measurement: Atlas's "profiled uniqueness from a sample" case becomes measured
uniqueness, which is never sample-bounded, and a measured non-unique pair is many_to_many rather than
unknown. The discovery-level cases (measured many-to-many and composite fixtures, cardinality never from
a model) are at the end.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.skills.relationships import (  # noqa: E402
    CORROBORATED,
    NAME_MATCH_ONLY,
    ColumnFacts,
    DeclaredForeignKey,
    RelationshipAssessment,
    RelationshipFacts,
    TableFacts,
    assess_relationship,
    discover_relationships,
)
from skills_fixtures import duck_dataset  # noqa: E402

SOURCE_TABLE, TARGET_TABLE = "s.source", "s.target"


def _col(name, *, nullable=False, physical_type="INTEGER", nulls=None, non_null=None, distinct=None) -> ColumnFacts:
    return ColumnFacts(name=name, physical_type=physical_type, nullable=nullable, null_count=nulls,
                       non_null_count=non_null, distinct_count=distinct)


def _facts(source, target, *, source_table=None, target_table=None, rule="EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
           observed=0, containment=None) -> RelationshipFacts:
    return RelationshipFacts(source=source_table or TableFacts(SOURCE_TABLE), target=target_table or TableFacts(TARGET_TABLE),
                             pairs=((source, target),), detection_rule=rule, observed_join_count=observed,
                             containment=containment)


def _classes(a: RelationshipAssessment) -> dict[str, bool]:
    return {c.name: c.corroborating for c in a.evidence_classes}


def _keyed(*columns: str) -> TableFacts:
    return TableFacts(TARGET_TABLE, declared_keys=(frozenset(columns),))


def test_a_name_and_type_match_alone_is_not_evidence_of_a_join():
    a = assess_relationship(_facts(_col("customer_id"), _col("customer_id")))
    assert a.outcome == NAME_MATCH_ONLY and not a.approvable
    assert _classes(a) == {"NAME_MATCH": False, "TYPE_MATCH": False}
    assert (a.cardinality, a.direction) == ("unknown", "undetermined")
    assert "FAN_OUT_POSSIBLE" in a.warnings


def test_a_key_on_the_target_alone_makes_a_corroborated_many_to_one_join():
    a = assess_relationship(_facts(_col("customer_id"), _col("customer_id"), target_table=_keyed("customer_id")))
    assert a.outcome == CORROBORATED and _classes(a)["DECLARED_KEY"] is True
    assert (a.cardinality, a.direction) == ("many_to_one", "source_references_target")
    assert a.join_condition == "source.customer_id = target.customer_id"
    assert a.warnings == ()


def test_two_keys_sharing_a_name_and_a_key_on_a_bare_id_are_not_evidence():
    both_keys = assess_relationship(_facts(_col("account_id"), _col("account_id"),
                                           source_table=TableFacts(SOURCE_TABLE, declared_keys=(frozenset({"account_id"}),)),
                                           target_table=_keyed("account_id")))
    bare_id = assess_relationship(_facts(_col("id"), _col("ID"), target_table=_keyed("id")))
    assert (both_keys.outcome, both_keys.cardinality) == (NAME_MATCH_ONLY, "one_to_one")
    assert bare_id.outcome == NAME_MATCH_ONLY and "GENERIC_COLUMN_NAME" in bare_id.warnings


def test_measured_uniqueness_corroborates_and_is_not_sample_bounded():
    target = _col("customer_id", nulls=0, non_null=1_000, distinct=1_000)
    a = assess_relationship(_facts(_col("customer_id", non_null=5_000, distinct=900), target))
    assert a.outcome == CORROBORATED
    (measured,) = [c for c in a.evidence_classes if c.name == "MEASURED_UNIQUE"]
    assert measured.corroborating and a.target_uniqueness.basis == "MEASURED"
    assert "UNIQUENESS_SAMPLE_BOUNDED" not in a.warnings  # Atlas flags sampled profiles; a measurement is exact
    near = assess_relationship(_facts(_col("customer_id"), _col("customer_id", non_null=1_000, distinct=995)))
    assert near.outcome == NAME_MATCH_ONLY  # Atlas accepted 99.5% distinct from a sample; measured, it is not unique


def test_a_foreign_key_declared_the_other_way_reverses_the_direction():
    source_table = TableFacts(SOURCE_TABLE, declared_keys=(frozenset({"customer_id"}),))
    target_table = TableFacts(TARGET_TABLE, foreign_keys=(DeclaredForeignKey(("customer_id",), SOURCE_TABLE, ("customer_id",)),))
    referencing = _col("customer_id", nullable=True, nulls=4, non_null=996)
    a = assess_relationship(_facts(_col("customer_id"), referencing, source_table=source_table, target_table=target_table))
    assert _classes(a)["DECLARED_FOREIGN_KEY"] is True
    assert (a.cardinality, a.direction, a.referencing_side) == ("one_to_many", "target_references_source", "target")
    assert a.optionality == "optional" and "DIRECTION_REVERSED" in a.warnings


def test_joins_observed_in_query_history_corroborate_a_join():
    a = assess_relationship(_facts(_col("cust_ref"), _col("customer_id", physical_type="VARCHAR(20)"),
                                   rule="QUERY_LOG_JOIN_V1", observed=7))
    assert a.outcome == CORROBORATED and _classes(a) == {"OBSERVED_QUERY_JOIN": True}
    assert {"TYPE_FAMILY_MISMATCH", "FAN_OUT_POSSIBLE"} <= set(a.warnings)


@pytest.mark.parametrize(("source", "expected"), [
    (_col("customer_id", nullable=False), "mandatory"),
    (_col("customer_id", nullable=True, nulls=3, non_null=997), "optional"),
    (_col("customer_id", nullable=True, nulls=0, non_null=1_000), "nullable_none_observed"),
    (_col("customer_id", nullable=True), "unknown"),
])
def test_optionality_of_the_referencing_columns_is_declared_and_observed(source, expected):
    assert assess_relationship(_facts(source, _col("customer_id"), target_table=_keyed("customer_id"))).optionality == expected


def test_measured_non_unique_sides_are_many_to_many_and_orphans_are_flagged():
    a = assess_relationship(_facts(_col("customer_id", non_null=10, distinct=4), _col("customer_id", non_null=8, distinct=3),
                                   containment=0.8))
    assert (a.cardinality, a.outcome) == ("many_to_many", NAME_MATCH_ONLY)
    assert {"FAN_OUT_POSSIBLE", "ORPHAN_VALUES"} <= set(a.warnings)


def test_full_containment_with_one_key_corroborates_but_not_on_a_generic_name():
    keyed = assess_relationship(_facts(_col("customer_id"), _col("customer_id"), target_table=_keyed("customer_id"), containment=1.0))
    assert _classes(keyed)["MEASURED_CONTAINMENT"] is True
    generic = assess_relationship(_facts(_col("code"), _col("code"), target_table=_keyed("code"), containment=1.0))
    assert _classes(generic)["MEASURED_CONTAINMENT"] is False and generic.outcome == NAME_MATCH_ONLY


# ------------------------------------------------------------------------------------ discovery level
def test_discovered_candidates_carry_the_assessment_and_measured_cardinality():
    duck = duck_dataset()
    group = {"asset": "itsm.sys_user_group", "columns": [{"name": "sys_id", "data_type": "VARCHAR", "is_key": True},
                                                         {"name": "name", "data_type": "VARCHAR"}]}
    incident = {"asset": "itsm.incident", "columns": [{"name": "assignment_group", "data_type": "VARCHAR",
                                                       "references": "itsm.sys_user_group.sys_id"}]}
    (rel,) = discover_relationships(duck, [incident, group])
    assert rel.cardinality == "many_to_one" and rel.assessment["cardinality"] == "many_to_one"
    assert rel.assessment["outcome"] == CORROBORATED
    assert {c["name"] for c in rel.assessment["evidence_classes"] if c["corroborating"]} >= {"DECLARED_FOREIGN_KEY"}


def test_many_to_many_is_detected_by_measurement():
    duck = duck_dataset()
    duck.con.execute("CREATE TABLE crm.tags AS SELECT id % 5 AS customer_id, 'x' AS tag FROM crm.customer")
    tags = {"asset": "crm.tags", "columns": [{"name": "customer_id", "data_type": "BIGINT"}, {"name": "tag", "data_type": "VARCHAR"}]}
    orders = {"asset": "crm.orders", "columns": [{"name": "customer_id", "data_type": "BIGINT", "references": "crm.tags.customer_id"}]}
    (rel,) = discover_relationships(duck, [orders, tags])
    assert rel.cardinality == "many_to_many" and rel.assessment["cardinality"] == "many_to_many"
    # the declared reference names a column that is not unique: the measurement wins, and says so
    assert {"FAN_OUT_POSSIBLE", "DECLARED_KEY_NOT_UNIQUE"} <= set(rel.assessment["warnings"])
    assert rel.assessment["outcome"] == CORROBORATED  # the declaration still says which table is referenced
