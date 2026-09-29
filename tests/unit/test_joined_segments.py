"""P8-16: an investigation segments by a related table's attribute (customer region, product category) and
tests a planned-vs-actual date outcome (delivered later than promised), so causes one join away are found.

Contract, validation (joins only along validated many-to-one lookups, in scope, no denied column), SQL per
dialect through the real gateway validator, unchanged row counts, the rule playbook, and an in-process
investigation on the retail journey's generated data (same generator and seed as scripts/e2e_full_journey.py)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import retail_fixtures as rf  # noqa: E402
from analystos import methods  # noqa: E402
from analystos.agents import investigator as inv  # noqa: E402
from analystos.agents.prompts import prompt  # noqa: E402
from analystos.agents.sql_agent import derivation_alias  # noqa: E402
from analystos.capabilities import packs  # noqa: E402
from analystos.contracts.analysis import AnalysisSpec, Derivation  # noqa: E402
from analystos.contracts.policy import DataScope  # noqa: E402
from analystos.core.errors import SQLRejected  # noqa: E402
from analystos.gateway.validator import validate_sql  # noqa: E402
from analystos.registries.hypotheses import spec_hash  # noqa: E402
from analystos.skills import lookups as lk  # noqa: E402
from analystos.skills import sqlbuild as sb  # noqa: E402
from analystos.skills import stats  # noqa: E402
from analystos.skills.analysis import run_analysis, verify_analysis  # noqa: E402
from analystos.skills.lookups import Lookup  # noqa: E402
from skills_fixtures import DuckRunSQL, GatewayRunSQL, TsqlViaDuckRunSQL  # noqa: E402

ORDERS, CUSTOMERS, PRODUCTS = "shop.orders", "shop.customers", "shop.products"
COLUMNS = {ORDERS: ["order_id", "customer_id", "product_id", "order_date", "quantity", "unit_price", "discount_pct",
                    "sales_channel", "status", "promised_date", "delivered_date", "returned"],
           CUSTOMERS: ["customer_id", "customer_name", "email", "region", "segment", "signup_date"],
           PRODUCTS: ["product_id", "product_name", "category", "list_price"]}
TYPES = {ORDERS: {"order_id": "id", "customer_id": "id", "product_id": "id", "order_date": "datetime", "quantity": "numeric",
                  "unit_price": "numeric", "discount_pct": "numeric", "sales_channel": "categorical", "status": "categorical",
                  "promised_date": "datetime", "delivered_date": "datetime", "returned": "categorical"},
         CUSTOMERS: {"customer_id": "id", "customer_name": "id", "email": "id", "region": "categorical", "segment": "categorical",
                     "signup_date": "datetime"},
         PRODUCTS: {"product_id": "id", "product_name": "id", "category": "categorical", "list_price": "numeric"}}
LOOKUPS = [Lookup(ORDERS, "customer_id", CUSTOMERS, "customer_id"), Lookup(ORDERS, "product_id", PRODUCTS, "product_id")]
CUSTOMER_JOIN = {"from_column": "customer_id", "asset": CUSTOMERS, "to_column": "customer_id"}
PRODUCT_JOIN = {"from_column": "product_id", "asset": PRODUCTS, "to_column": "product_id"}
LATE = {"type": "later_than", "column": "promised_date", "end_column": "delivered_date", "label": "delivered later than promised"}
RETURNED = {"type": "equals", "column": "returned", "value": "Yes", "label": "returned"}


def scope(dialect: str = "duckdb", *, denied: list[str] = (), assets: list[str] | None = None,
          sources: dict[str, str] | None = None) -> DataScope:
    assets = assets or list(COLUMNS)
    return DataScope(workspace_id="ws", user_id="u", role="analyst", source_ids=["src", "other"], assets=assets,
                     asset_sources=sources or {a: "src" for a in assets}, columns={a: COLUMNS[a] for a in assets},
                     denied_columns=list(denied), source_dialects={"src": dialect, "other": dialect})


def spec(**kw) -> AnalysisSpec:
    base = {"method": "rate_by_segment", "asset": ORDERS, "outcome": LATE,
            "segment": {"type": "column", "column": "region", "via": "customer_id", "label": "customer region"},
            "joins": [CUSTOMER_JOIN]}
    base.update(kw)
    return AnalysisSpec.model_validate(base)


# ------------------------------------------------------------------------------------ contract
def test_via_must_name_a_declared_join():
    with pytest.raises(ValidationError, match="names no join"):
        spec(joins=[])
    with pytest.raises(ValidationError, match="own from_column"):
        spec(joins=[CUSTOMER_JOIN, CUSTOMER_JOIN])
    s = spec(filters=[{"column": "category", "op": "=", "value": "Electronics", "via": "product_id"}],
             joins=[CUSTOMER_JOIN, PRODUCT_JOIN])
    assert s.vias() == ["customer_id", "product_id"] and s.table_of(s.segment) == CUSTOMERS and s.table_of(s.outcome) == ORDERS
    assert s.table_of(s.filters[0]) == PRODUCTS


def test_a_spec_without_the_new_fields_serializes_and_hashes_as_before():
    legacy = {"method": "rate_by_segment", "asset": "a.t", "outcome": {"type": "is_true", "column": "x", "end_column": None,
              "value": None, "edges": None, "grain": None, "start_hour": 8, "end_hour": 18, "label": None},
              "segment": {"type": "column", "column": "y", "end_column": None, "value": None, "edges": None, "grain": None,
                          "start_hour": 8, "end_hour": 18, "label": None},
              "drivers": [], "time": None, "filters": [{"column": "z", "op": "=", "value": 1, "origin": "plan"}],
              "min_group_size": 30, "top_k": 12}
    dumped = AnalysisSpec.model_validate(legacy).model_dump()
    assert dumped == legacy and spec_hash(dumped) == spec_hash(legacy)
    assert "via" in spec().model_dump()["segment"] and spec().model_dump()["joins"] == [CUSTOMER_JOIN]


def test_json_schema_exports_the_new_vocabulary():
    schema = AnalysisSpec.model_json_schema()
    assert "later_than" in schema["$defs"]["Derivation"]["properties"]["type"]["enum"]
    assert {"via", "tolerance_hours"} <= set(schema["$defs"]["Derivation"]["properties"])
    assert "joins" in schema["properties"] and "via" in schema["$defs"]["Filter"]["properties"]
    assert AnalysisSpec.model_json_schema(mode="serialization") == schema  # compat serializer keeps the schema


# ------------------------------------------------------------------------------------ validation
def test_a_join_along_a_validated_lookup_is_accepted():
    assert inv.validate_spec(spec(), scope(), TYPES, LOOKUPS) == []
    assert methods.get("rate_by_segment").validate(spec(), lambda d: None) == []


@pytest.mark.parametrize("case,errors", [
    ("no_lookups", "not a validated many-to-one relationship"),
    ("wrong_key", "not a validated many-to-one relationship"),
    ("out_of_scope", "joined table shop.customers is not in the authorized scope"),
    ("other_source", "in another source"),
    ("denied_attribute", "restricted column region"),
    ("denied_everywhere", "restricted column region"),
    ("denied_key", "restricted join column shop.customers.customer_id"),
    ("unknown_column", "unknown column shop.customers.nope"),
    ("not_a_date", "later_than compares two dates; status is not a date/time column"),
])
def test_joins_and_columns_that_are_rejected(case, errors):
    s, sc, lookups = spec(), scope(), LOOKUPS
    if case == "no_lookups":
        lookups = []
    elif case == "wrong_key":
        s = spec(joins=[{**CUSTOMER_JOIN, "to_column": "customer_name"}])
    elif case == "out_of_scope":
        sc = scope(assets=[ORDERS, PRODUCTS])
    elif case == "other_source":
        sc = scope(sources={ORDERS: "src", CUSTOMERS: "other", PRODUCTS: "src"})
    elif case == "denied_attribute":
        sc = scope(denied=["shop.customers.region"])
    elif case == "denied_everywhere":
        sc = scope(denied=["*.region"])
    elif case == "denied_key":
        sc = scope(denied=["shop.customers.customer_id"])
    elif case == "unknown_column":
        s = spec(segment={"type": "column", "column": "nope", "via": "customer_id"})
    elif case == "not_a_date":
        s = spec(outcome={"type": "later_than", "column": "promised_date", "end_column": "status"})
    got = inv.validate_spec(s, sc, TYPES, lookups)
    assert got and any(errors in e for e in got), got


def test_a_user_redirect_is_not_mistaken_for_a_joined_filter():
    s = spec(filters=[{"column": "segment", "op": "=", "value": "Consumer", "via": "customer_id"}])
    out = inv.with_constraints(s, {"filters": [{"column": "segment", "op": "=", "value": "Consumer"}]})
    assert [(f.via, f.origin) for f in out.filters] == [("customer_id", "plan"), (None, "user_redirect")]


def test_denied_filter_on_a_related_table_is_rejected():
    s = spec(filters=[{"column": "segment", "op": "=", "value": "Consumer", "via": "customer_id"}])
    assert inv.validate_spec(s, scope(), TYPES, LOOKUPS) == []
    assert any("restricted filter column segment" in e
               for e in inv.validate_spec(s, scope(denied=["shop.customers.segment"]), TYPES, LOOKUPS))


def test_a_joined_numeric_segment_must_be_bucketed_like_a_local_one():
    s = spec(method="rate_by_segment", segment={"type": "column", "column": "list_price", "via": "product_id"}, joins=[PRODUCT_JOIN])
    assert any("must be bucketed" in e for e in inv.validate_spec(s, scope(), TYPES, LOOKUPS))


def test_complete_joins_adds_the_join_from_the_lookups_and_plain_labels():
    raw = {"method": "rate_by_segment", "asset": ORDERS, "outcome": {"type": "later_than", "column": "promised_date",
                                                                    "end_column": "delivered_date"},
           "segment": {"type": "column", "column": "region", "via": "customer_id"}}
    done, errors = lk.complete_joins(raw, LOOKUPS)
    assert errors == [] and done["joins"] == [CUSTOMER_JOIN] and "joins" not in raw
    assert done["segment"]["label"] == "customer region" and done["outcome"]["label"] == "delivered later than promised"
    assert inv.validate_spec(AnalysisSpec.model_validate(done), scope(), TYPES, LOOKUPS) == []
    _, errors = lk.complete_joins({**raw, "segment": {"type": "column", "column": "region", "via": "sales_channel"}}, LOOKUPS)
    assert errors == ["no validated many-to-one relationship from shop.orders.sales_channel"]
    plain, errors = lk.complete_joins({"method": "trend", "asset": ORDERS, "time": {"type": "date_trunc", "column": "order_date"}}, LOOKUPS)
    assert errors == [] and "joins" not in plain
    unused, errors = lk.complete_joins({**raw, "segment": {"type": "column", "column": "sales_channel"},
                                        "joins": [CUSTOMER_JOIN]}, LOOKUPS)
    assert errors == [] and "joins" not in unused  # a join nothing reads: the same test as without it


def test_accept_completes_a_model_proposal_and_rejects_an_unvalidated_join(monkeypatch):
    monkeypatch.setattr(inv, "validated_lookups", lambda ctx: LOOKUPS)
    ctx = SimpleNamespace(scope=scope())
    model = {"statement": "Late deliveries differ by the customer's region",
             "spec": {"method": "rate_by_segment", "asset": ORDERS, "outcome": {"type": "later_than", "column": "promised_date",
                                                                               "end_column": "delivered_date"},
                      "segment": {"type": "column", "column": "region", "via": "customer_id"}}}
    invented = {"statement": "Returns differ by a made-up join",
                "spec": {**model["spec"], "segment": {"type": "column", "column": "region", "via": "customer_id"},
                         "joins": [{"from_column": "customer_id", "asset": CUSTOMERS, "to_column": "email"}]}}
    unknown = {"statement": "Via a column that references nothing", "spec": {**model["spec"],
               "segment": {"type": "column", "column": "region", "via": "status"}}}
    accepted, rejected = inv._accept(ctx, [model, invented, unknown], TYPES, origin="agent", seen=set())
    assert [a["statement"] for a in accepted] == [model["statement"]]
    assert accepted[0]["spec"]["joins"] == [CUSTOMER_JOIN] and accepted[0]["spec"]["segment"]["label"] == "customer region"
    reasons = {r["statement"]: r["reason"] for r in rejected}
    assert "not a validated many-to-one relationship" in reasons[invented["statement"]]
    assert reasons[unknown["statement"]] == "no validated many-to-one relationship from shop.orders.status"


# ------------------------------------------------------------------------------------ SQL
@pytest.mark.parametrize("dialect", ["postgres", "tsql", "duckdb"])
@pytest.mark.parametrize("method", ["rate_by_segment", "driver_model", "contribution_decomposition", "numeric_by_segment", "pareto"])
def test_joined_specs_compile_per_dialect_and_pass_the_gateway(dialect, method):
    s = spec(filters=[{"column": "category", "op": "=", "value": "Electronics", "via": "product_id"},
                      {"column": "status", "op": "=", "value": "Delivered"}], joins=[CUSTOMER_JOIN, PRODUCT_JOIN])
    if method == "driver_model":
        s = s.model_copy(update={"segment": None, "drivers": [s.segment, Derivation(column="sales_channel")]})
    elif method == "contribution_decomposition":
        s = s.model_copy(update={"time": Derivation(type="date_trunc", column="order_date", grain="month")})
    elif method == "numeric_by_segment":
        s = s.model_copy(update={"outcome": Derivation(column="quantity")})
    elif method == "pareto":
        s = s.model_copy(update={"outcome": None})
    s = AnalysisSpec.model_validate({**s.model_dump(), "method": method})
    for purpose in sb.METHOD_PURPOSES[method]:
        cq = sb.compile_spec(s, dialect, purpose=purpose, sample_rows=500)
        assert sb.parses(cq.sql, dialect) and cq.sql.count("LEFT JOIN") == 2
        v = validate_sql(scope(dialect), cq.sql, max_rows=cq.max_rows or 1000)
        assert set(v.referenced_assets) == {ORDERS, CUSTOMERS, PRODUCTS}


def test_the_gateway_still_rejects_what_validation_would():
    cq = sb.compile_spec(spec(), "postgres")
    with pytest.raises(SQLRejected):
        validate_sql(scope("postgres", assets=[ORDERS, PRODUCTS]), cq.sql, max_rows=1000)
    with pytest.raises(SQLRejected):
        validate_sql(scope("postgres", denied=["shop.customers.region"]), cq.sql, max_rows=1000)


def test_later_than_compiles_per_dialect():
    d = Derivation(**LATE)
    assert sb.render_derivation(d, "postgres") == ('CASE WHEN "promised_date" IS NULL OR "delivered_date" IS NULL THEN NULL WHEN '
                                                   'CAST("delivered_date" AS TIMESTAMP) > CAST("promised_date" AS TIMESTAMP) '
                                                   'THEN 1 ELSE 0 END')
    assert "DATETIME2" in sb.render_derivation(d, "tsql")
    tol = d.model_copy(update={"tolerance_hours": 24.0})
    assert "EXTRACT(EPOCH FROM" in sb.render_derivation(tol, "postgres") and "> 24.0" in sb.render_derivation(tol, "postgres")
    assert "DATEDIFF_BIG(SECOND" in sb.render_derivation(tol, "tsql")
    joined = Derivation(type="column", column="region", via="customer_id")
    assert sb.render_derivation(joined, "postgres") == '"j_customer_id"."region"'
    assert sb.describe(joined) == "customer_id->region" and sb.describe(Derivation(**{**LATE, "label": None})) == \
        "delivered_date>promised_date"


def test_a_spec_without_joins_compiles_exactly_as_before():
    plain = AnalysisSpec(method="rate_by_segment", asset=ORDERS, outcome=Derivation(**RETURNED),
                         segment=Derivation(column="sales_channel"))
    sql = sb.compile_spec(plain, "postgres").sql
    assert 'FROM "shop"."orders" WHERE' not in sql and 'FROM "shop"."orders")' in sql and '"t"' not in sql and "JOIN" not in sql


# ------------------------------------------------------------------------------------ data (DuckDB)
@pytest.fixture(scope="module")
def retail():
    import duckdb

    con = duckdb.connect()
    data = rf.generate()
    rf.load_duckdb(con, data)
    duck = DuckRunSQL(con)
    return SimpleNamespace(duck=duck, data=data, truth=rf.truth(data), catalog=rf.catalog(duck))


@pytest.mark.parametrize("dialect", ["duckdb", "tsql"])
def test_row_counts_do_not_change_because_of_the_join(retail, dialect):
    inner = retail.duck if dialect == "duckdb" else TsqlViaDuckRunSQL(retail.duck)
    gw = GatewayRunSQL(inner, scope(dialect))
    joined = spec(joins=[CUSTOMER_JOIN, PRODUCT_JOIN], filters=[])
    rows = gw(sb.compile_spec(joined, dialect).sql).records()
    assert sum(r["n_rows"] for r in rows) == 6000 and {r["segment"] for r in rows} == {"North", "South", "East", "West"}
    both = joined.model_copy(update={"segment": Derivation(column="category", via="product_id")})
    assert sum(r["n_rows"] for r in gw(sb.compile_spec(both, dialect).sql).records()) == 6000
    num = AnalysisSpec.model_validate({**joined.model_dump(), "method": "numeric_by_segment", "outcome": {"column": "quantity"}})
    assert sum(r["n_rows"] for r in gw(sb.compile_spec(num, dialect, purpose="summary").sql).records()) == 6000
    drv = AnalysisSpec.model_validate({**joined.model_dump(), "method": "driver_model", "segment": None,
                                       "drivers": [joined.segment.model_dump(), {"column": "sales_channel"}]})
    sample = gw(sb.compile_spec(drv, dialect, sample_rows=100000).sql).records()
    assert sample[0]["_rows_total"] == 6000 and len(sample) == 6000


def test_a_non_unique_key_is_never_a_lookup(retail):
    """Only a measured many-to-one relationship becomes a lookup: a key with duplicates would multiply rows."""
    from analystos.skills.relationships import discover_relationships

    retail.duck.con.execute('CREATE TABLE "shop"."customers_dup" AS SELECT * FROM "shop"."customers" '
                            'UNION ALL SELECT * FROM "shop"."customers" WHERE "region" = \'West\'')
    cols = [{"name": c, "data_type": t} for c, t in rf.column_types(retail.data["customers"]).items()]
    orders = [{"name": c, "data_type": t} for c, t in rf.column_types(retail.data["orders"]).items()]
    rels = discover_relationships(retail.duck, [{"asset": ORDERS, "columns": orders},
                                                {"asset": "shop.customers_dup", "columns": cols}])
    assert not [r for r in rels if r.to_asset == "shop.customers_dup" and r.cardinality == "many_to_one"]


def test_catalog_measures_the_two_lookups(retail):
    assert set(retail.catalog["lookups"]) == set(LOOKUPS)
    assert retail.catalog["types"] == TYPES
    assert {c.name: c.flag_true for c in retail.catalog["cols"][ORDERS]}["returned"] == "Yes"


# ------------------------------------------------------------------------------------ playbook
def test_planned_actual_pairs_are_domain_neutral():
    assert lk.planned_actual_pairs(["order_date", "promised_date", "delivered_date"]) == [("promised_date", "delivered_date")]
    assert lk.planned_actual_pairs(["opened_at", "due_date", "resolved_at", "closed_at"]) == [("due_date", "resolved_at")]
    assert lk.planned_actual_pairs(["expected_ship_date", "delivered_at", "ship_date"]) == [("expected_ship_date", "ship_date")]
    assert lk.planned_actual_pairs(["created_at", "updated_at", "closed_at"]) == []
    assert lk.later_than_label("due_date", "resolved_at") == "resolved later than due"
    assert lk.joined_label("customer_id", "region") == "customer region"
    assert lk.joined_label("customer_id", "customer_name") == "customer name"
    assert lk.joined_label("assignment_group", "name") == "assignment group name"


def _proposals(catalog, pack_ids=("pack.sales",)):
    chosen = [p for p in packs.installed() if p.id in pack_ids]
    related = [(x, catalog["cols"][x.to_asset]) for x in catalog["lookups"] if x.from_asset == ORDERS]
    return inv.proposals_for_table(ORDERS, catalog["cols"][ORDERS], chosen, related=related)


def test_playbook_proposes_joined_segments_and_a_late_outcome(retail):
    props = _proposals(retail.catalog)
    joined = [p for p in props if (p["spec"].get("segment") or {}).get("via")]
    assert {(p["spec"]["segment"]["via"], p["spec"]["segment"]["column"]) for p in joined} == \
        {("customer_id", "region"), ("customer_id", "segment"), ("product_id", "category")}
    assert all(p["priority"] == "high" and p["spec"]["joins"] for p in joined)
    late = [p for p in props if (p["spec"].get("outcome") or {}).get("type") == "later_than"]
    assert late and {p["spec"]["outcome"]["end_column"] for p in late} == {"delivered_date"}
    assert "The rate of delivered later than promised differs materially across customer region." in [p["statement"] for p in late]
    flag = [p for p in props if (p["spec"].get("outcome") or {}).get("column") == "returned"]
    assert flag and all(p["spec"]["outcome"] == {**p["spec"]["outcome"], "type": "equals", "value": "Yes"} for p in flag)


def test_servicenow_due_vs_resolved_is_a_late_outcome():
    from analystos.skills import hypothesis_templates as tmpl

    cols = [tmpl.Col("number", "id", "identifier", {"distinct": 5000}), tmpl.Col("priority", "categorical", "dimension", {"distinct": 4}),
            tmpl.Col("opened_at", "datetime", "timestamp"), tmpl.Col("due_date", "datetime", "date"),
            tmpl.Col("resolved_at", "datetime", "timestamp")]
    props = inv.proposals_for_table("itsm.incident", cols, [])
    late = [p["spec"]["outcome"] for p in props if (p["spec"].get("outcome") or {}).get("type") == "later_than"]
    assert late and late[0]["column"] == "due_date" and late[0]["end_column"] == "resolved_at"
    assert late[0]["label"] == "resolved later than due"


def _round_one(catalog, sc):
    """What `generate_hypotheses` does in rules mode: complete, validate, dedupe, rank by the rule priority, cap."""
    accepted, seen = [], set()
    for p in _proposals(catalog):
        raw, errors = lk.complete_joins(p["spec"], catalog["lookups"])
        s = AnalysisSpec.model_validate(raw)
        assert not errors and inv.validate_spec(s, sc, catalog["types"], catalog["lookups"]) == [], p["statement"]
        keys = inv.identity_keys(s)
        if keys & seen:
            continue
        seen |= keys
        accepted.append({**p, "spec": s.model_dump(), "priority_score": {"high": 1.0, "medium": 0.5, "low": 0.0}[p["priority"]]})
    accepted.sort(key=lambda a: -a["priority_score"])
    from analystos.services.platform_settings import PlatformSettings

    return inv.diverse_top(accepted, PlatformSettings().analysis.max_round1_hypotheses)


def test_investigation_finds_the_causes_one_join_away(retail):
    """Round 1 on the generated data, every statement through the gateway: the West late-delivery effect and the
    Electronics return effect are found and independently verified; the no-effect segments are rejected; the
    round-2 drill-down into Electronics finds the Marketplace interaction."""
    sc = scope()
    gw = GatewayRunSQL(retail.duck, sc)
    top = _round_one(retail.catalog, sc)
    assert len(top) == 8
    results = {}
    for a in top:
        s = AnalysisSpec.model_validate(a["spec"])
        results[(s.outcome.column if s.outcome else None, methods.get(s.method).claim_subject(a["spec"]))] = (s, run_analysis(s, gw))
    padj = dict(zip(results, stats.benjamini_hochberg([o.stat.p_value if o.stat.p_value is not None else 1.0
                                                       for _, o in results.values()]), strict=True))

    late_s, late = results[("promised_date", "customer_id.region")]
    assert late.stat.supported and late.stat.highlights["top_segment"] == "West" and padj[("promised_date", "customer_id.region")] < 0.05
    assert late.stat.highlights["top_rate"] == pytest.approx(retail.truth["late_west"], abs=1e-3)
    assert verify_analysis(late_s, gw, late.stat).agrees
    ret_s, ret = results[("returned", "product_id.category")]
    assert ret.stat.supported and ret.stat.highlights["top_segment"] == "Electronics" and padj[("returned", "product_id.category")] < 0.05
    assert ret.stat.highlights["top_rate"] == pytest.approx(retail.truth["returned_electronics"], abs=1e-3)
    assert verify_analysis(ret_s, gw, ret.stat).agrees
    for no_effect in (("returned", "customer_id.region"), ("promised_date", "product_id.category"), ("returned", "customer_id.segment")):
        assert results[no_effect][1].stat.supported is False, no_effect

    title, text = methods.get("rate_by_segment").template_text(late_s.model_dump(), late.stat.model_dump())
    assert title == "Delivered later than promised concentrates in customer region = West"
    assert text.startswith("Records with customer region = West have a delivered later than promised rate of 14.8%")

    supported = [{"code": "H-2", "spec": ret_s.model_dump(), "result": {"highlights": ret.stat.highlights}}]
    drill = inv._drilldowns(supported, retail.catalog["types"])
    assert drill and drill[0]["spec"]["filters"][-1] == {"column": "category", "op": "=", "value": "Electronics", "via": "product_id"}
    d_spec = AnalysisSpec.model_validate(drill[0]["spec"])
    assert inv.validate_spec(d_spec, sc, retail.catalog["types"], retail.catalog["lookups"]) == []
    inside = run_analysis(d_spec, gw)
    assert inside.stat.supported and inside.stat.highlights["top_segment"] == "Marketplace"


def test_matrix_continues_an_outcome_across_joined_dimensions():
    tested = spec(outcome=RETURNED, segment={"type": "column", "column": "sales_channel", "label": "sales channel"}, joins=[])
    results = [{"code": "H-1", "status": "rejected", "spec": tested.model_dump()}]
    joined = {ORDERS: lk.joined_dimensions([(LOOKUPS[1], [_col("product_id", "id"), _col("category", "categorical", 4)])])}
    types = {ORDERS: {"sales_channel": "categorical", "returned": "categorical"}}
    nxt = inv._matrix_continuations(results, types, [], {}, joined)
    assert len(nxt) == 1 and nxt[0]["spec"]["segment"]["via"] == "product_id" and nxt[0]["spec"]["joins"] == [PRODUCT_JOIN]
    assert nxt[0]["statement"] == "Returned differs materially across product category."
    assert inv.validate_spec(AnalysisSpec.model_validate(nxt[0]["spec"]), scope(), TYPES, LOOKUPS) == []


def _col(name, semantic, distinct=None):
    from analystos.skills import hypothesis_templates as tmpl

    return tmpl.Col(name, semantic, None, {"distinct": distinct} if distinct else {})


# ------------------------------------------------------------------------------------ narrative, dataset, prompt
def test_dataset_aliases_name_the_joined_attribute():
    assert derivation_alias(Derivation(column="region", via="customer_id")) == "customer_region"
    assert derivation_alias(Derivation(column="customer_name", via="customer_id")) == "customer_name"
    assert derivation_alias(Derivation(**LATE)) == "delivered_date_after_promised_date"
    assert derivation_alias(Derivation(column="region")) == "region"


def test_claim_identity_separates_a_joined_attribute_from_a_local_one():
    rate = methods.get("rate_by_segment")
    joined = rate.claim_key(spec().model_dump(), {"top_segment": "West"})
    local = rate.claim_key(spec(segment={"type": "column", "column": "region"}, joins=[]).model_dump(), {"top_segment": "West"})
    assert joined != local and joined[2] == "customer_id.region"


def test_prompts_teach_via_and_later_than():
    for name in ("hypothesis_generation.v1", "follow_up_generation.v1"):
        text = prompt(name)
        assert "later_than" in text and '"via"' in text and "related_tables" in text
    rows = lk.for_prompt([(LOOKUPS[0], [_col("customer_id", "id"), _col("email", "id"), _col("region", "categorical", 4)])])
    assert rows == [{"asset": ORDERS, "via": "customer_id", "references": "shop.customers.customer_id", "columns": ["region"]}]
