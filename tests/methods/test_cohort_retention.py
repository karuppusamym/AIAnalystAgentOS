"""cohort_retention planted-effect benchmark (P4-X04): synthetic activity with a KNOWN retention drop
for the later cohorts, and a null control where every cohort retains alike. Runs through the real
gateway validator on DuckDB and on T-SQL (transpiled to DuckDB), deterministic seeds, no services."""
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
from analystos.evidence.facts import bind_finding
from analystos.gateway.validator import validate_sql
from analystos.services.changes import claim_key
from analystos.skills import sqlbuild as sb
from analystos.skills.analysis import run_analysis, verify_analysis

COHORTS, PER_COHORT, END = 12, 300, 16  # cohorts Jan..Dec 2025, activity observed through Apr 2026
COLUMNS = ["customer_id", "event_at", "amount"]


def _month(i: int) -> dt.datetime:
    return dt.datetime(2025 + i // 12, i % 12 + 1, 1)


def make_activity(retention: list[float], seed: int) -> pd.DataFrame:
    """Each customer is active in its cohort month, then stays active month to month with the cohort's
    retention probability (geometric churn), with 1-3 events per active month."""
    rng = np.random.default_rng(seed)
    rows = []
    for c in range(COHORTS):
        for k in range(PER_COHORT):
            cid = f"C{c:02d}{k:04d}"
            m = c
            while m < END:
                start = _month(m)
                days = (_month(m + 1) - start).days
                for _ in range(int(rng.integers(1, 4))):
                    rows.append((cid, start + dt.timedelta(seconds=int(rng.integers(0, days * 86400))), float(rng.gamma(2, 20))))
                if rng.random() >= retention[c]:
                    break
                m += 1
    rows += [(None, _month(3), 10.0)] * 5  # NULL entity rows are excluded
    return pd.DataFrame(rows, columns=COLUMNS)


PLANTED = [0.6] * 6 + [0.35] * 6  # the H2 cohorts churn faster
NULL = [0.5] * COHORTS


def _duck(seed: int) -> DuckRunSQL:
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA bench")
    for name, ret in (("planted", PLANTED), ("null", NULL)):
        con.register("_tmp", make_activity(ret, seed))
        con.execute(f"CREATE TABLE bench.activity_{name} AS SELECT * FROM _tmp")
        con.unregister("_tmp")
    return DuckRunSQL(con)


def scope(dialect: str) -> DataScope:
    assets = {f"bench.activity_{n}": COLUMNS for n in ("planted", "null")}
    return DataScope(workspace_id="ws", user_id="u", role="analyst", source_ids=["src"], assets=list(assets),
                     asset_sources={a: "src" for a in assets}, columns=assets, source_dialects={"src": dialect})


def spec(table: str, **kw) -> AnalysisSpec:
    return AnalysisSpec(method="cohort_retention", asset=f"bench.activity_{table}", outcome=Derivation(column="customer_id", label="customers"),
                        time=Derivation(type="date_trunc", column="event_at", grain="month"), **kw)


@pytest.fixture(scope="module", params=[1, 2])
def duck(request):
    return _duck(request.param)


@pytest.mark.parametrize("dialect", ["duckdb", "tsql"])
def test_planted_retention_drop_found_and_verified(duck, dialect):
    gw = GatewayRunSQL(duck if dialect == "duckdb" else TsqlViaDuckRunSQL(duck), scope(dialect))
    prim = run_analysis(spec("planted"), gw)
    s = prim.stat
    assert s.supported is True, s
    assert s.method == "cohort_retention" and s.highlights["n_cohorts"] == COHORTS
    early = {_month(i).date().isoformat() for i in range(6)}
    assert s.highlights["top_segment"] in early and s.highlights["baseline_segment"] not in early
    rates = {g["segment"]: g["rate"] for g in s.groups}
    assert 0.55 <= np.mean([rates[c] for c in early]) <= 0.65  # the quoted retention is unbiased for the planted 0.6
    assert 0.30 <= np.mean([r for c, r in rates.items() if c not in early]) <= 0.40
    ver = verify_analysis(spec("planted"), gw, s)
    assert ver.agrees is True and ver.stat.supported is True, ver.stat
    assert prim.table["columns"][:3] == ["cohort", "cohort_size", "retention_1"] and len(prim.table["rows"]) == COHORTS
    assert len(prim.query_ids) == 1 and gw.calls[0]["purpose"] == "analysis.cohort_retention.primary"


@pytest.mark.parametrize("dialect", ["duckdb", "tsql"])
def test_null_control_rejected_and_verification_agrees(duck, dialect):
    gw = GatewayRunSQL(duck if dialect == "duckdb" else TsqlViaDuckRunSQL(duck), scope(dialect))
    prim = run_analysis(spec("null"), gw)
    assert prim.stat.supported is False, prim.stat
    ver = verify_analysis(spec("null"), gw, prim.stat)
    assert ver.stat.supported is False and ver.agrees is True


def test_censored_cohort_is_excluded(duck):
    """With activity cut at the last cohort's own month, that cohort's next month is unobserved."""
    s = run_analysis(spec("planted", filters=[{"column": "event_at", "op": "<", "value": "2026-01-01"}]), duck).stat
    assert s.highlights["n_cohorts"] == COHORTS - 1 and any("not observed yet" in w for w in s.warnings)


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "duckdb"])
def test_compiled_sql_passes_the_gateway(dialect):
    cq = sb.compile_spec(spec("planted"), dialect)
    v = validate_sql(scope(dialect), cq.sql, max_rows=cq.max_rows)
    assert v.referenced_assets == ["bench.activity_planted"] and cq.kind == "aggregate"
    assert "customer_id" not in cq.columns.values()  # entity ids never leave the source


def test_derived_artefacts(duck):
    """Validation, template (numbers-guard safe), claim key and chart intent all come from the module."""
    sc = scope("duckdb")
    types = {"bench.activity_planted": {"customer_id": "id", "event_at": "datetime", "amount": "numeric"}}
    assert validate_spec(spec("planted"), sc, types) == []
    bad = AnalysisSpec(method="cohort_retention", asset="bench.activity_planted", outcome=Derivation(column="amount"),
                       segment=Derivation(column="customer_id"))
    errs = validate_spec(bad, sc, types)
    assert any("not an entity identifier" in e for e in errs) and any("date_trunc" in e for e in errs)
    stat = run_analysis(spec("planted"), duck).stat.model_dump()
    sp = spec("planted").model_dump()
    title, text = template_text(stat, sp)
    assert "cohort" in title and stat["highlights"]["top_segment"] in text
    assert _guard(title + " " + text, facts_for(stat, sp))
    assert bind_finding(sp, stat, methods.get("cohort_retention").facts(sp, stat), title, text).ok  # P4-03 typed binding
    key = claim_key(sp, stat["highlights"])
    assert key[0] == "cohort_retention" and key[2] == "cohort:event_at:month" and key[-1] == stat["highlights"]["top_segment"]
    assert methods.get("cohort_retention").chart_intent(spec("planted")).dimension == "cohort"
