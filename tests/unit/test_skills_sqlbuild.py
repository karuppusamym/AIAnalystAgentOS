"""compile_spec: every method x derivation in duckdb end to end, semantics vs pandas ground truth,
and postgres / tsql SQL that parses (and transpiles)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
import sqlglot

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.contracts.analysis import AnalysisSpec, Derivation, Filter  # noqa: E402
from analystos.core.errors import InvalidInput  # noqa: E402
from analystos.skills import sqlbuild as sb  # noqa: E402
from skills_fixtures import duck_dataset, make_incidents  # noqa: E402

D = Derivation
A = "itsm.incident"

DERIVATIONS = {
    "column": D(column="priority"),
    "duration_hours": D(type="duration_hours", column="opened_at", end_column="resolved_at"),
    "after_hours": D(type="after_hours", column="opened_at"),
    "bucket": D(type="bucket", column="reassignment_count", edges=[0, 1, 2, 3]),
    "equals": D(type="equals", column="priority", value="1 - Critical"),
    "equals_bool": D(type="equals", column="sla_breached", value=True),
    "is_true": D(type="is_true", column="sla_breached"),
    "is_true_text": D(type="is_true", column="made_sla"),
    "date_trunc": D(type="date_trunc", column="opened_at", grain="week"),
    "hour_of_day": D(type="hour_of_day", column="opened_at"),
    "day_of_week": D(type="day_of_week", column="opened_at"),
}
NUMERIC_OUT = D(type="duration_hours", column="opened_at", end_column="resolved_at")
BOOL_OUT = D(type="is_true", column="sla_breached")
FILTERS = [Filter(column="priority", op="!=", value="4 - Low"),
           Filter(column="category", op="in", value=["Network", "Software", "Hardware", "Database"], origin="user_redirect"),
           Filter(column="resolved_at", op="is not null")]


def all_specs() -> list[tuple[str, AnalysisSpec]]:
    out = []
    for name, d in DERIVATIONS.items():
        out.append((f"rate/{name}", AnalysisSpec(method="rate_by_segment", asset=A, outcome=BOOL_OUT, segment=d, filters=FILTERS)))
        out.append((f"numeric/{name}", AnalysisSpec(method="numeric_by_segment", asset=A, outcome=NUMERIC_OUT, segment=d)))
        out.append((f"pareto/{name}", AnalysisSpec(method="pareto", asset=A, segment=d, filters=FILTERS[:1])))
    for name in ("equals", "equals_bool", "is_true", "is_true_text", "after_hours", "column"):
        out.append((f"rate_outcome/{name}", AnalysisSpec(method="rate_by_segment", asset=A, outcome=DERIVATIONS[name],
                                                         segment=D(column="noise_group"))))
    for name in ("duration_hours", "hour_of_day", "day_of_week", "is_true", "after_hours"):
        out.append((f"trend_outcome/{name}", AnalysisSpec(method="trend", asset=A, outcome=DERIVATIONS[name],
                                                          time=D(type="date_trunc", column="opened_at", grain="month"))))
    for grain in ("day", "week", "month", "quarter"):
        out.append((f"trend/{grain}", AnalysisSpec(method="trend", asset=A, time=D(type="date_trunc", column="opened_at", grain=grain))))
    out.append(("trend/raw_column", AnalysisSpec(method="trend", asset=A, time=D(column="opened_at"))))
    out.append(("pareto/sum_outcome", AnalysisSpec(method="pareto", asset=A, segment=D(column="app"), outcome=BOOL_OUT)))
    out.append(("correlation", AnalysisSpec(method="correlation", asset=A, outcome=NUMERIC_OUT,
                                            drivers=[D(column="reassignment_count")], filters=FILTERS)))
    out.append(("correlation/hour", AnalysisSpec(method="correlation", asset=A, outcome=NUMERIC_OUT,
                                                 drivers=[DERIVATIONS["hour_of_day"]])))
    out.append(("driver_model", AnalysisSpec(method="driver_model", asset=A, outcome=BOOL_OUT, drivers=[
        DERIVATIONS[k] for k in ("column", "bucket", "after_hours", "hour_of_day", "day_of_week", "duration_hours", "equals")])))
    out.append(("driver_model/text_outcome", AnalysisSpec(method="driver_model", asset=A, outcome=D(column="made_sla"),
                                                          drivers=[D(column="reassignment_count")])))
    return out


SPECS = all_specs()


@pytest.fixture(scope="module")
def duck():
    return duck_dataset()


@pytest.fixture(scope="module")
def df():
    return make_incidents()


@pytest.mark.parametrize("name,spec", SPECS, ids=[n for n, _ in SPECS])
def test_every_spec_executes_in_duckdb(duck, name, spec):
    for purpose in sb.METHOD_PURPOSES[spec.method]:
        cq = sb.compile_spec(spec, "duckdb", purpose=purpose, sample_rows=500)
        res = duck(cq.sql, purpose="test", max_rows=cq.max_rows)
        assert res.row_count > 0, name
        for alias in cq.columns.values():
            assert alias in res.columns
        if cq.kind == "sample":
            assert res.row_count <= 500


@pytest.mark.parametrize("dialect", ["postgres", "tsql"])
@pytest.mark.parametrize("name,spec", SPECS, ids=[n for n, _ in SPECS])
def test_other_dialects_parse_and_transpile(dialect, name, spec):
    for purpose in sb.METHOD_PURPOSES[spec.method]:
        sql = sb.compile_spec(spec, dialect, purpose=purpose, sample_rows=500).sql
        tree = sqlglot.parse_one(sql, read=dialect)
        assert tree is not None
        # the gateway may re-generate the statement; the round trip must still parse
        again = tree.sql(dialect=dialect)
        assert sqlglot.parse_one(again, read=dialect) is not None
        if dialect == "tsql":
            assert " LIMIT " not in sql and "TOP " in sql
            assert "DATE_TRUNC" not in sql and "EXTRACT(" not in sql
        else:
            assert "DATEPART" not in sql


def test_deterministic_sample(duck):
    spec = AnalysisSpec(method="numeric_by_segment", asset=A, outcome=NUMERIC_OUT, segment=DERIVATIONS["after_hours"])
    cq = sb.compile_spec(spec, "duckdb", sample_rows=300)
    a, b = duck(cq.sql).rows, duck(cq.sql).rows
    assert a == b and len(a) == 300
    total, excluded = a[0][2], a[0][3]
    assert total == 6000 and excluded == 281  # rows with NULL resolved_at are excluded and counted


# ------------------------------------------------------------------ semantics vs pandas ground truth
def _dow_sun0(ts: pd.Series) -> pd.Series:
    return (ts.dt.weekday + 1) % 7


def _q(duck, d: Derivation, extra_where: str = "") -> pd.Series:
    expr = sb.render_derivation(d, "duckdb")
    rows = duck(f'SELECT "number", {expr} AS v FROM "itsm"."incident" ORDER BY "sys_id"').rows
    return pd.Series([r[1] for r in rows])


def test_derivation_semantics_match_pandas(duck, df):
    d = df.sort_values("sys_id").reset_index(drop=True)
    ts = pd.to_datetime(d["opened_at"])
    assert (_q(duck, DERIVATIONS["hour_of_day"]).astype(int) == ts.dt.hour).all()
    assert (_q(duck, DERIVATIONS["day_of_week"]).astype(int) == _dow_sun0(ts)).all()
    ah = ((ts.dt.hour < 8) | (ts.dt.hour >= 18) | (ts.dt.weekday >= 5)).astype(int)
    assert (_q(duck, DERIVATIONS["after_hours"]) == ah).all()
    dur = (pd.to_datetime(d["resolved_at"]) - ts).dt.total_seconds() / 3600
    got = _q(duck, NUMERIC_OUT)
    assert got.isna().sum() == dur.isna().sum()
    assert (got.dropna() - dur.dropna()).abs().max() < 1e-3
    week = _q(duck, DERIVATIONS["date_trunc"])
    assert (pd.to_datetime(week) == ts.dt.to_period("W-SUN").dt.start_time).all()  # Monday-start weeks
    rc = d["reassignment_count"]
    exp_b = rc.map(lambda v: str(v) if v < 3 else "3+")
    assert (_q(duck, DERIVATIONS["bucket"]) == exp_b).all()
    assert (_q(duck, DERIVATIONS["is_true_text"]) == (d["made_sla"] == "true").astype(int)).all()
    assert (_q(duck, DERIVATIONS["equals_bool"]) == d["sla_breached"].astype(int)).all()
    assert (_q(duck, DERIVATIONS["equals"]) == (d["priority"] == "1 - Critical").astype(int)).all()


def test_rate_counts_match_pandas(duck, df):
    spec = AnalysisSpec(method="rate_by_segment", asset=A, outcome=BOOL_OUT, segment=DERIVATIONS["bucket"],
                        filters=[Filter(column="priority", op="!=", value="4 - Low")])
    rows = duck(sb.compile_spec(spec, "duckdb").sql).records()
    sub = df[df["priority"] != "4 - Low"]
    lab = sub["reassignment_count"].map(lambda v: str(v) if v < 3 else "3+")
    for r in rows:
        m = lab == r["segment"]
        assert r["n"] == m.sum() and r["positives"] == sub.loc[m, "sla_breached"].sum()
        assert r["segment_order"] == (3 if r["segment"] == "3+" else int(r["segment"]))


# ------------------------------------------------------------------ builders, escaping, errors
def test_bucket_labels_sort_as_strings():
    assert sb.bucket_labels([0, 1, 2, 3]) == ["0", "1", "2", "3+"]
    labels = sb.bucket_labels([0, 5, 10, 20, 100])
    assert labels == sorted(labels) == ["000-005", "005-010", "010-020", "020-100", "100+"]
    assert sb.below_label([0, 5, 10]) < sb.bucket_labels([0, 5, 10])[0]


def test_render_helpers_qualify_and_escape():
    for dialect, q in (("postgres", '"t"."opened_at"'), ("duckdb", '"t"."opened_at"'), ("tsql", "[t].[opened_at]")):
        s = sb.render_derivation(DERIVATIONS["after_hours"], dialect, table_alias="t")
        assert q in s and sqlglot.parse_one(f"SELECT {s} FROM x AS t", read=dialect)
    f = sb.render_filter(Filter(column="name", op="=", value="O'Brien'; DROP TABLE x; --"), "postgres", table_alias="t")
    assert f == """"t"."name" = 'O''Brien''; DROP TABLE x; --'"""
    assert sb.render_filter(Filter(column="flag", op="=", value=True), "tsql") == "[flag] = 1"
    assert sb.render_filter(Filter(column="x", op="not in", value=[1, 2]), "duckdb") == 'NOT "x" IN (1, 2)'
    assert sb.render_filter(Filter(column="x", op="is null"), "duckdb") == '"x" IS NULL'
    weird = sb.render_derivation(D(column='we"ird col'), "postgres")
    assert weird == '"we""ird col"'


def test_injection_values_do_not_change_statement(duck):
    spec = AnalysisSpec(method="pareto", asset=A, segment=D(column="app"),
                        filters=[Filter(column="priority", op="=", value="x'); DROP TABLE itsm.incident; --")])
    res = duck(sb.compile_spec(spec, "duckdb").sql)
    assert res.row_count == 0
    assert duck('SELECT COUNT(*) FROM "itsm"."incident"').rows[0][0] == 6000


@pytest.mark.parametrize("bad", [
    AnalysisSpec(method="rate_by_segment", asset=A, segment=D(column="x")),
    AnalysisSpec(method="numeric_by_segment", asset=A, outcome=NUMERIC_OUT),
    AnalysisSpec(method="trend", asset=A),
    AnalysisSpec(method="driver_model", asset=A, outcome=BOOL_OUT),
    AnalysisSpec(method="rate_by_segment", asset=A, outcome=BOOL_OUT, segment=D(type="bucket", column="x", edges=[3, 1])),
    AnalysisSpec(method="rate_by_segment", asset=A, outcome=D(type="duration_hours", column="a"), segment=D(column="x")),
    AnalysisSpec(method="pareto", asset="a.b.c.d", segment=D(column="x")),
    AnalysisSpec(method="pareto", asset=A, segment=D(column="x"), filters=[Filter(column="y", op="=", value=None)]),
])
def test_invalid_specs_raise(bad):
    with pytest.raises(InvalidInput):
        sb.compile_spec(bad, "postgres")


def test_unknown_dialect_and_purpose():
    spec = AnalysisSpec(method="pareto", asset=A, segment=D(column="app"))
    with pytest.raises(InvalidInput):
        sb.compile_spec(spec, "oracle")
    with pytest.raises(InvalidInput):
        sb.compile_spec(spec, "duckdb", purpose="summary")
    assert set(sb.compile_all(spec, "mssql")) == {"primary"}
