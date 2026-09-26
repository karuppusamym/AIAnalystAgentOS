"""dbt column lineage (P6-07): Atlas's LN-5 suite, ported as the spec, plus manifest-level cases.

Ported from AIDataAnalyst@8b48fd9:tests/test_dbt_column_lineage.py (13 cases) onto
`analystos.evidence.lineage.dbt`. Only the fixture builder changes (`ParsedDbtResource` ->
`DbtResource`, `DependencyResource` is the same type). The manifest cases after the port restate
AIDataAnalyst@8b48fd9:tests/test_dbt_column_lineage_integration.py without its database double:
relations map to node ids, a stale dependency or unparseable SQL yields no column edges. New: catalog
columns resolve an unqualified reference, and a model's CTE is traced to its dependency.
"""

from __future__ import annotations

from analystos.evidence.lineage.dbt import (
    MAX_COLUMN_EDGES_PER_RESOURCE,
    DbtResource,
    extract_column_lineage,
    manifest_lineage,
)
from analystos.evidence.lineage.dbt import DbtResource as DependencyResource


def _resource(
    *,
    unique_id: str = "model.bank.customer_summary",
    compiled_sql_redacted: str | None,
    sql_parse_status: str = "PARSED",
    depends_on_unique_ids: list[str] | None = None,
) -> DbtResource:
    return DbtResource(
        unique_id=unique_id,
        name="customer_summary",
        database_name="bank",
        schema_name="analytics",
        relation_name='"bank"."analytics"."customer_summary"',
        compiled_sql_redacted=compiled_sql_redacted,
        compiled_sql_hash="deadbeef",
        sql_parse_status=sql_parse_status,
        column_names=("customer_id", "name"),
        depends_on_unique_ids=tuple(depends_on_unique_ids or ["model.bank.stg_customers"]),
    )


def _stg_customers_dependency(**overrides: object) -> DependencyResource:
    fields: dict[str, object] = {
        "unique_id": "model.bank.stg_customers",
        "relation_name": '"analytics"."staging"."stg_customers"',
        "database_name": "analytics",
        "schema_name": "staging",
        "name": "stg_customers",
    }
    fields.update(overrides)
    return DependencyResource(**fields)  # type: ignore[arg-type]


class TestResolvableReference:
    def test_relation_name_match_produces_column_edges_with_passthrough(self) -> None:
        resource = _resource(
            compiled_sql_redacted=(
                "SELECT c.id AS customer_id, c.name "
                "FROM analytics.staging.stg_customers AS c"
            )
        )
        dependency = _stg_customers_dependency()

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert len(edges) == 2
        by_target = {edge.target_column: edge for edge in edges}
        assert by_target["customer_id"].source_column == "id"
        assert by_target["customer_id"].source_unique_id == dependency.unique_id
        assert by_target["customer_id"].transformation_type == "DIRECT"
        assert by_target["customer_id"].confidence == "FULL"
        assert by_target["name"].source_column == "name"
        assert by_target["name"].source_unique_id == dependency.unique_id

    def test_bare_name_match_when_relation_name_and_composite_are_absent(self) -> None:
        resource = _resource(
            compiled_sql_redacted=(
                "SELECT stg_customers.id AS customer_id FROM stg_customers"
            )
        )
        dependency = _stg_customers_dependency(
            relation_name=None, database_name=None, schema_name=None
        )

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert len(edges) == 1
        assert edges[0].source_unique_id == dependency.unique_id
        assert edges[0].source_column == "id"
        assert edges[0].target_column == "customer_id"

    def test_composite_key_matches_when_relation_name_absent(self) -> None:
        resource = _resource(
            compiled_sql_redacted=(
                "SELECT c.id AS customer_id "
                "FROM analytics.staging.stg_customers AS c"
            )
        )
        dependency = _stg_customers_dependency(relation_name=None)

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert len(edges) == 1
        assert edges[0].source_unique_id == dependency.unique_id

    def test_match_is_case_insensitive_and_quote_insensitive(self) -> None:
        resource = _resource(
            compiled_sql_redacted=(
                'SELECT c.id AS customer_id '
                'FROM "ANALYTICS"."STAGING"."STG_CUSTOMERS" AS c'
            )
        )
        dependency = _stg_customers_dependency()

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert len(edges) == 1
        assert edges[0].source_unique_id == dependency.unique_id


class TestUnresolvableReferenceIsDropped:
    def test_reference_outside_declared_dependencies_produces_no_edge(self) -> None:
        resource = _resource(
            compiled_sql_redacted="SELECT o.id FROM external_system.other_table AS o",
        )
        dependency = _stg_customers_dependency()

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert edges == []

    def test_unqualified_column_in_single_table_select_is_not_guessed(self) -> None:
        # sql_lineage_parser cannot attribute an unqualified column to its
        # source table on its own (see module docstring); extract_column_lineage
        # must not paper over that by assuming "the only declared dependency".
        resource = _resource(
            compiled_sql_redacted="SELECT id AS customer_id FROM analytics.staging.stg_customers",
        )
        dependency = _stg_customers_dependency()

        edges = extract_column_lineage(resource, [dependency], "postgres")

        assert edges == []

    def test_no_dependencies_at_all_produces_no_edges(self) -> None:
        resource = _resource(
            compiled_sql_redacted="SELECT c.id FROM analytics.staging.stg_customers AS c",
            depends_on_unique_ids=[],
        )

        edges = extract_column_lineage(resource, [], "postgres")

        assert edges == []


class TestUnparsedResourceProducesNoEdges:
    def test_unparseable_status_short_circuits(self) -> None:
        resource = _resource(
            compiled_sql_redacted="SELECT c.id FROM analytics.staging.stg_customers AS c",
            sql_parse_status="UNPARSEABLE",
        )

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        assert edges == []

    def test_not_present_status_short_circuits(self) -> None:
        resource = _resource(compiled_sql_redacted=None, sql_parse_status="NOT_PRESENT")

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        assert edges == []

    def test_too_large_status_short_circuits(self) -> None:
        resource = _resource(compiled_sql_redacted=None, sql_parse_status="TOO_LARGE")

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        assert edges == []


class TestStarExpansionIsSkipped:
    def test_select_star_produces_no_fabricated_edge(self) -> None:
        resource = _resource(
            compiled_sql_redacted="SELECT * FROM analytics.staging.stg_customers AS c",
        )

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        # sql_lineage_parser itself no longer silently drops a `SELECT *` --
        # it records honest table-level `TABLE_STAR` evidence (AT-D2). That
        # evidence still produces no *column-level* edge here: it is not a
        # column-to-column dependency, so extract_column_lineage filters it
        # out rather than fabricating one (see its module docstring).
        assert edges == []

    def test_mixed_star_and_explicit_columns_only_emits_the_explicit_one(self) -> None:
        resource = _resource(
            compiled_sql_redacted=(
                "SELECT *, c.id AS customer_id FROM analytics.staging.stg_customers AS c"
            )
        )

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        assert len(edges) == 1
        assert edges[0].target_column == "customer_id"


class TestBounding:
    def test_edges_are_capped_per_resource(self) -> None:
        columns = ", ".join(f"c.col_{i}" for i in range(MAX_COLUMN_EDGES_PER_RESOURCE + 50))
        resource = _resource(
            compiled_sql_redacted=(
                f"SELECT {columns} FROM analytics.staging.stg_customers AS c"  # noqa: S608
            )
        )

        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")

        assert len(edges) == MAX_COLUMN_EDGES_PER_RESOURCE


# --- manifest level (restates Atlas's LN-5 import cases without its database double) -------------


def manifest_fixture() -> dict:
    return {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json"},
        "sources": {},
        "nodes": {
            "model.bank.stg_customers": {
                "resource_type": "model", "package_name": "bank", "name": "stg_customers", "alias": "stg_customers",
                "database": "analytics", "schema": "staging", "relation_name": '"analytics"."staging"."stg_customers"',
                "config": {"materialized": "view"}, "columns": {}, "depends_on": {"nodes": []},
                "compiled_code": "SELECT r.id, r.name FROM raw.customers AS r WHERE r.email <> 'x@y.z'",
            },
            "model.bank.customer_summary": {
                "resource_type": "model", "package_name": "bank", "name": "customer_summary", "alias": "customer_summary",
                "database": "bank", "schema": "analytics", "relation_name": '"bank"."analytics"."customer_summary"',
                "config": {"materialized": "table"}, "columns": {}, "depends_on": {"nodes": ["model.bank.stg_customers"]},
                "compiled_code": "SELECT c.id AS customer_id, c.name FROM analytics.staging.stg_customers AS c",
            },
        },
    }


class TestManifestLineage:
    def test_relations_map_to_node_ids_with_table_and_column_edges(self) -> None:
        out = manifest_lineage(manifest_fixture())
        assert out["table_edges"] == [{"source_unique_id": "model.bank.stg_customers",
                                       "target_unique_id": "model.bank.customer_summary"}]
        by_target = {e["target_column"]: e for e in out["column_edges"]}
        assert set(by_target) == {"customer_id", "name"}
        assert by_target["customer_id"]["source_unique_id"] == "model.bank.stg_customers"
        assert by_target["customer_id"]["target_unique_id"] == "model.bank.customer_summary"
        assert by_target["customer_id"]["source_column"] == "id"
        assert by_target["customer_id"]["transformation_type"] == "DIRECT"
        assert by_target["customer_id"]["confidence"] == "FULL"
        # raw.customers is not a manifest resource: no fabricated edge for stg_customers
        assert all(e["target_unique_id"] != "model.bank.stg_customers" for e in out["column_edges"])

    def test_stale_dependency_gets_its_table_edge_but_no_column_edges(self) -> None:
        manifest = manifest_fixture()
        manifest["nodes"]["model.bank.customer_summary"]["compiled_code"] = (
            "SELECT o.id AS customer_id FROM other_system.unrelated_table AS o")
        out = manifest_lineage(manifest)
        assert len(out["table_edges"]) == 1
        assert out["column_edges"] == []

    def test_unparseable_compiled_sql_is_marked_and_yields_no_column_edges(self) -> None:
        manifest = manifest_fixture()
        manifest["nodes"]["model.bank.customer_summary"]["compiled_code"] = "SELECT FROM ("
        out = manifest_lineage(manifest)
        assert out["resources"]["model.bank.customer_summary"] == "UNPARSEABLE"
        assert out["column_edges"] == []

    def test_compiled_sql_is_redacted_before_it_is_kept(self) -> None:
        from analystos.evidence.lineage.dbt import resources_from_manifest

        res = resources_from_manifest(manifest_fixture())["model.bank.stg_customers"]
        assert "x@y.z" not in (res.compiled_sql_redacted or "") and "<REDACTED>" in (res.compiled_sql_redacted or "")


class TestNewInAnalystOS:
    def test_catalog_columns_resolve_an_unqualified_reference(self) -> None:
        resource = _resource(compiled_sql_redacted="SELECT id AS customer_id FROM analytics.staging.stg_customers")
        dependency = _stg_customers_dependency()
        assert extract_column_lineage(resource, [dependency], "postgres") == []
        edges = extract_column_lineage(resource, [dependency], "postgres",
                                       catalog_columns={dependency.unique_id: ["id", "name"]})
        assert [(e.source_unique_id, e.source_column, e.target_column) for e in edges] == [
            (dependency.unique_id, "id", "customer_id")]

    def test_build_events_carry_validated_column_lineage_and_redacted_sql(self) -> None:
        from datetime import UTC, datetime

        from analystos.build.lineage import openlineage_events
        from analystos.evidence.schemas import validate_openlineage

        manifest = {"nodes": {"model.p.ds": {
            "resource_type": "model", "name": "ds", "schema": "mart", "depends_on": {"nodes": ["source.p.s.incident"]},
            "columns": {}, "compiled_code": ('WITH base AS (SELECT i.number, i.priority FROM "analytics"."s"."incident" AS i '
                                             "WHERE i.state = 'closed') SELECT b.number, COUNT(*) AS n, MAX(b.priority) AS "
                                             "top FROM base AS b GROUP BY b.number")}},
            "sources": {"source.p.s.incident": {"resource_type": "source", "schema": "s", "name": "incident",
                                                "database": "analytics"}}}
        now = datetime(2026, 9, 26, tzinfo=UTC)
        events = openlineage_events(job_id="bld_1", workspace_id="ws", manifest=manifest,
                                    run_results={"results": [{"unique_id": "model.p.ds", "status": "success"}]},
                                    namespace="postgres://h:5432", database="analytics", started_at=now, finished_at=now)
        done = events[1]
        assert all(validate_openlineage(e) == [] for e in events)
        fields = done["outputs"][0]["facets"]["columnLineage"]["fields"]
        assert fields["number"]["inputFields"][0] == {"namespace": "postgres://h:5432", "name": "analytics.s.incident",
                                                     "field": "number",
                                                     "transformations": [{"type": "DIRECT", "subtype": "IDENTITY"}]}
        assert fields["top"]["inputFields"][0]["transformations"][0]["subtype"] == "AGGREGATION"
        assert "closed" not in done["job"]["facets"]["sql"]["query"]

    def test_a_model_cte_is_traced_to_its_dependency(self) -> None:
        resource = _resource(compiled_sql_redacted=(
            "WITH base AS (SELECT c.id, c.name FROM analytics.staging.stg_customers AS c) "
            "SELECT b.id AS customer_id, UPPER(b.name) AS name FROM base AS b"))
        edges = extract_column_lineage(resource, [_stg_customers_dependency()], "postgres")
        by_target = {e.target_column: e for e in edges}
        assert by_target["customer_id"].source_column == "id"
        assert by_target["name"].transformation_type == "DERIVED"
        assert {e.source_unique_id for e in edges} == {"model.bank.stg_customers"}
