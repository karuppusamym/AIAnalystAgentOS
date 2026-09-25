"""contribution_decomposition planted-effect benchmark (P4-X04): synthetic orders over three months in
which the web channel's share jumps in the last month (a mix shift in every outcome). Planted:
the web return rate doubles and the web order amount rises 30% (within-segment changes). Null
controls: `late` and `basket` differ by channel but never change within a channel, so their KPIs move
through mix alone and the method must not claim a within-segment change. Deterministic seeds; runs
through the real gateway validator on DuckDB and on T-SQL (transpiled to DuckDB); no services."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
from tests.skills_fixtures import DuckRunSQL, GatewayRunSQL, TsqlViaDuckRunSQL

from analystos import methods
from analystos.agents.insight import _guard, facts_for, template_text
from analystos.agents.investigator import validate_spec
from analystos.contracts.analysis import AnalysisSpec, Derivation
from analystos.contracts.policy import DataScope
from analystos.gateway.validator import validate_sql
from analystos.services.changes import claim_key
from analystos.skills import sqlbuild as sb
from analystos.skills.analysis import run_analysis, verify_analysis

CHANNELS = ["web", "store", "phone", "partner"]
SHARES = {0: [0.25] * 4, 1: [0.25] * 4, 2: [0.46, 0.18, 0.18, 0.18]}  # the last month shifts mix to web
PER_MONTH = 4000
COLUMNS = ["order_at", "channel", "returned", "late", "amount", "basket"]
LATE = {"web": 0.35, "store": 0.10, "phone": 0.15, "partner": 0.10}  # constant within channel
BASKET = {"web": 150.0, "store": 80.0, "phone": 90.0, "partner": 80.0}


def make_orders(seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for m, shares in SHARES.items():
        start = dt.datetime(2026, 1 + m, 1)
        for ch in rng.choice(CHANNELS, size=PER_MONTH, p=shares):
            planted = m == 2 and ch == "web"
            rows.append((start + dt.timedelta(seconds=int(rng.integers(0, 28 * 86400))), str(ch),
                         bool(rng.random() < (0.30 if planted else 0.15)), bool(rng.random() < LATE[ch]),
                         float(rng.normal(130.0 if planted else 100.0, 30.0)), float(rng.normal(BASKET[ch], 20.0))))
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.loc[:9, "amount"] = None  # NULL outcomes are counted and excluded
    return df


@pytest.fixture(scope="module", params=[1, 2, 3])
def duck(request):
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA bench")
    con.register("_tmp", make_orders(request.param))
    con.execute("CREATE TABLE bench.orders AS SELECT * FROM _tmp")
    con.unregister("_tmp")
    return DuckRunSQL(con)


def scope(dialect: str) -> DataScope:
    return DataScope(workspace_id="ws", user_id="u", role="analyst", source_ids=["src"], assets=["bench.orders"],
                     asset_sources={"bench.orders": "src"}, columns={"bench.orders": COLUMNS}, source_dialects={"src": dialect})


def spec(outcome: Derivation) -> AnalysisSpec:
    return AnalysisSpec(method="contribution_decomposition", asset="bench.orders", outcome=outcome,
                        segment=Derivation(column="channel"), time=Derivation(type="date_trunc", column="order_at", grain="month"))


PLANTED = {"return_rate": spec(Derivation(type="is_true", column="returned", label="return rate")),
           "order_amount": spec(Derivation(column="amount", label="order amount"))}
NULLS = {"late_rate_mix_only": spec(Derivation(type="is_true", column="late", label="late delivery")),
         "basket_mix_only": spec(Derivation(column="basket", label="basket value"))}


def _gw(duck, dialect):
    return GatewayRunSQL(duck if dialect == "duckdb" else TsqlViaDuckRunSQL(duck), scope(dialect))


@pytest.mark.parametrize("dialect", ["duckdb", "tsql"])
@pytest.mark.parametrize("name", list(PLANTED))
def test_planted_within_segment_change_found_and_verified(duck, dialect, name):
    gw = _gw(duck, dialect)
    prim = run_analysis(PLANTED[name], gw)
    s = prim.stat
    assert s.supported is True, (name, s)
    hl = s.highlights
    assert hl["top_segment"] == "web" and hl["within_segment_direction"] == "increase"
    assert hl["period_before"].startswith("2026-02") and hl["period_after"].startswith("2026-03")
    d = s.details
    kpi = d["kpi"]
    assert abs(d["rate_effect"] + d["mix_effect"] - (hl[f"{kpi}_after"] - hl[f"{kpi}_before"])) < 1e-3  # exact split
    web = next(g for g in s.groups if g["segment"] == "web")
    assert 0.40 <= web["share_after"] <= 0.52 and 0.22 <= web["share_before"] <= 0.28
    ver = verify_analysis(PLANTED[name], gw, s)
    assert ver.agrees is True and ver.stat.supported is True, (name, ver.stat)


@pytest.mark.parametrize("dialect", ["duckdb", "tsql"])
@pytest.mark.parametrize("name", list(NULLS))
def test_mix_only_change_is_not_claimed(duck, dialect, name):
    gw = _gw(duck, dialect)
    prim = run_analysis(NULLS[name], gw)
    s = prim.stat
    assert s.supported is False, (name, s)
    kpi = s.details["kpi"]
    assert s.highlights[f"{kpi}_after"] > s.highlights[f"{kpi}_before"]  # the KPI did move ...
    assert s.highlights["dominant_effect"] == "mix"  # ... through mix, which the decomposition attributes
    ver = verify_analysis(NULLS[name], gw, s)
    assert ver.stat.supported is False and ver.agrees is True, (name, ver.stat)


def test_null_outcomes_counted_and_small_segments_pooled(duck):
    s = run_analysis(PLANTED["order_amount"], duck).stat
    assert s.details["excluded_null_outcome_rows"] == 10
    pooled = run_analysis(PLANTED["return_rate"].model_copy(update={"top_k": 2}), duck).stat
    segs = {g["segment"] for g in pooled.groups}
    assert len(segs) == 3 and {"web", "(other)"} <= segs
    assert pooled.details["pooled_segments"] == 2 and pooled.highlights["top_segment"] == "web"


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "duckdb"])
def test_compiled_sql_passes_the_gateway(dialect):
    for s in (*PLANTED.values(), *NULLS.values()):
        cq = sb.compile_spec(s, dialect)
        v = validate_sql(scope(dialect), cq.sql, max_rows=cq.max_rows)
        assert v.referenced_assets == ["bench.orders"] and cq.kind == "aggregate"


@pytest.mark.parametrize("name", list(PLANTED))
def test_derived_artefacts(duck, name):
    """Validation, template (numbers-guard safe), claim key and chart intent all come from the module."""
    types = {"bench.orders": {"order_at": "datetime", "channel": "categorical", "returned": "boolean", "late": "boolean",
                              "amount": "numeric", "basket": "numeric"}}
    sc = scope("duckdb")
    assert validate_spec(PLANTED[name], sc, types) == []
    no_time = AnalysisSpec(method="contribution_decomposition", asset="bench.orders", outcome=Derivation(column="channel"),
                           segment=Derivation(column="channel"))
    errs = validate_spec(no_time, sc, types)
    assert any("not numeric" in e for e in errs) and any("date_trunc" in e for e in errs)
    stat = run_analysis(PLANTED[name], duck).stat.model_dump()
    sp = PLANTED[name].model_dump()
    title, text = template_text(stat, sp)
    assert "web" in title and "within channel groups" in text
    assert _guard(title + " " + text, facts_for(stat, sp)), text
    key = claim_key(sp, stat["highlights"])
    assert key[0] == "contribution_decomposition" and key[2] == "channel" and key[-1] == "web"
    intent = methods.get("contribution_decomposition").chart_intent(PLANTED[name])
    assert (intent.intent, intent.dimension) == ("comparison", "segment")
