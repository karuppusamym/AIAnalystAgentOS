"""P4-C12 without services: declared sampling, SQL per dialect, snapshot records, caveats and the
``representative_population`` check."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import polars as pl
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, oracle, postgresql, sqlite

from analystos.connectors import sampling
from analystos.connectors.base import DiscoveredAsset
from analystos.connectors.csv_file import CSVFileConnector
from analystos.connectors.generic_sql import GenericSQLConnector, _SampledTable
from analystos.core.errors import InvalidInput
from analystos.staging.snapshots import CHECK, population_caveat, population_check, stage_asset

# ------------------------------------------------------------------------------ declared strategy


def test_sampling_spec_validation() -> None:
    assert sampling.sampling_for({}) is None
    assert sampling.sampling_for({"sampling": "full"}).method == "full"
    spec = sampling.sampling_for({"sampling": {"method": "tablesample", "percent": 5, "seed": 7}})
    assert (spec.method, spec.percent, spec.seed, spec.sampler) == ("tablesample", 5, 7, None)
    for bad, message in [
        ({"method": "time_window", "column": "at"}, "time_window needs"),
        ({"method": "time_window", "column": "at", "window": "3 months"}, "must look like"),
        ({"method": "tablesample"}, "needs 'percent'"),
        ({"method": "tablesample", "percent": 100}, "percent"),
        ({"method": "full", "percent": 5}, "apply only to method tablesample"),
        ({"method": "first_n", "column": "at"}, "apply only to method time_window"),
        ({"method": "reservoir"}, "method"),
        ({"method": "full", "limit": 5}, "limit"),
    ]:
        with pytest.raises(InvalidInput, match=message):
            sampling.sampling_for({"sampling": bad})


def test_per_asset_override_merges_or_replaces() -> None:
    config = {"sampling": {"method": "tablesample", "percent": 5, "assets": {
        "sales.Orders": {"method": "time_window", "column": "placed_at", "window": "90d"},
        "items": {"seed": 3}}}}
    assert sampling.sampling_for(config, "sales.orders").method == "time_window"  # case-insensitive, replaced
    merged = sampling.sampling_for(config, "sales.items", "items")
    assert (merged.method, merged.percent, merged.seed) == ("tablesample", 5, 3)  # same method: merged
    assert sampling.sampling_for(config, "other").percent == 5
    assert sampling.sampling_for({"sampling": {"assets": {"a": "full"}}}, "b") is None


def test_validate_sampling_per_kind() -> None:
    sampling.validate_sampling("postgres", True, {"sampling": {"method": "tablesample", "percent": 1}}, "postgres")
    sampling.validate_sampling("servicenow", False, {"sampling": {"method": "first_n"}})
    with pytest.raises(InvalidInput, match="not available for servicenow"):
        sampling.validate_sampling("servicenow", False, {"sampling": {"method": "tablesample", "percent": 1}})
    with pytest.raises(InvalidInput, match="not available for csv"):
        sampling.validate_sampling("csv", False, {"sampling": {"assets": {
            "f": {"method": "time_window", "column": "t", "window": "7d"}}}})
    with pytest.raises(InvalidInput, match="only sampler: system"):
        sampling.validate_sampling("bigquery", True, {"sampling": {"method": "tablesample", "percent": 1,
                                                                   "sampler": "bernoulli"}}, "bigquery")
    with pytest.raises(InvalidInput, match="no block sampling"):
        sampling.validate_sampling("sqlite", True, {"sampling": {"method": "tablesample", "percent": 1,
                                                                 "sampler": "system"}}, "sqlite")
    with pytest.raises(InvalidInput, match="assets must map"):
        sampling.validate_sampling("postgres", True, {"sampling": {"method": "full", "assets": ["x"]}}, "postgres")


def test_row_cap() -> None:
    full = sampling.SamplingSpec(method="full")
    assert sampling.row_cap({"max_rows": 500}, 1_000_000, full) == 1_000_000  # full: only the admin limit
    assert sampling.row_cap({"max_rows": 500}, 1_000_000, None) == 500
    assert sampling.row_cap({"max_rows": 5_000_000}, 1_000_000, None) == 1_000_000  # never above the admin limit
    assert sampling.default_seed("sales.orders") == sampling.default_seed("sales.orders")


# ------------------------------------------------------------------------------ SQL per dialect


@pytest.mark.parametrize(
    ("dialect", "sampler", "suffix", "where", "native", "repeatable"),
    [
        ("postgres", None, "TABLESAMPLE BERNOULLI (2.5) REPEATABLE (7)", None, True, True),
        ("postgres", "system", "TABLESAMPLE SYSTEM (2.5) REPEATABLE (7)", None, True, True),
        ("duckdb", None, "TABLESAMPLE BERNOULLI (2.5 PERCENT) REPEATABLE (7)", None, True, True),
        ("snowflake", None, "TABLESAMPLE BERNOULLI (2.5) SEED (7)", None, True, True),
        ("trino", "system", "TABLESAMPLE SYSTEM (2.5)", None, True, False),
        ("oracle", None, "SAMPLE (2.5) SEED (7)", None, True, True),
        ("oracle", "system", "SAMPLE BLOCK (2.5) SEED (7)", None, True, True),
        ("databricks", None, "TABLESAMPLE (2.5 PERCENT) REPEATABLE (7)", None, True, True),
        ("tsql", None, "TABLESAMPLE SYSTEM (2.5 PERCENT) REPEATABLE (7)", None, True, True),
        ("bigquery", None, "TABLESAMPLE SYSTEM (2.5 PERCENT)", None, True, False),
        ("mysql", None, None, "RAND(7) < 0.025", False, True),
        ("redshift", None, None, "RANDOM() < 0.025", False, False),
        ("clickhouse", None, None, "randCanonical() < 0.025", False, False),
        ("sqlite", None, None, "(ABS(RANDOM()) % 1000000) < 25000", False, False),
    ],
)
def test_tablesample_sql_per_dialect(dialect, sampler, suffix, where, native, repeatable) -> None:
    out = sampling.tablesample_sql(dialect, 2.5, sampler, seed=7)
    assert (out.from_suffix, out.where, out.native, out.repeatable) == (suffix, where, native, repeatable)


def test_tablesample_rejections() -> None:
    with pytest.raises(InvalidInput, match="only sampler: system"):
        sampling.tablesample_sql("tsql", 5, "bernoulli", seed=1)
    with pytest.raises(InvalidInput, match="only sampler: bernoulli"):
        sampling.tablesample_sql("databricks", 5, "system", seed=1)
    with pytest.raises(InvalidInput, match="not supported for the teradata"):
        sampling.tablesample_sql("teradata", 5, None, seed=1)
    with pytest.raises(InvalidInput, match="less than 100"):
        sampling.tablesample_sql("postgres", 100, None, seed=1)


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        (postgresql.dialect(), 'FROM sales."Order Lines" TABLESAMPLE BERNOULLI (5) REPEATABLE (1)'),
        (oracle.dialect(), 'FROM sales."Order Lines" SAMPLE (5) SEED (1)'),
        (mssql.dialect(), "FROM sales.[Order Lines] TABLESAMPLE SYSTEM (5 PERCENT) REPEATABLE (1)"),
        (sqlite.dialect(), 'FROM sales."Order Lines" TABLESAMPLE'),
    ],
)
def test_sampled_table_compiles_after_the_from_reference(dialect, expected) -> None:
    clause = {"postgresql": "TABLESAMPLE BERNOULLI (5) REPEATABLE (1)", "oracle": "SAMPLE (5) SEED (1)",
              "mssql": "TABLESAMPLE SYSTEM (5 PERCENT) REPEATABLE (1)", "sqlite": "TABLESAMPLE"}[dialect.name]
    t = _SampledTable("Order Lines", sa.column("id"), schema="sales", suffix=clause)
    sql = " ".join(str(sa.select(t.c.id).where(t.c.id > 1).limit(10).compile(dialect=dialect)).split())
    assert expected in sql
    assert sql.count(clause) == 1  # only in FROM, never in column references


# ------------------------------------------------------------------------------ extraction (SQLite / DuckDB)


def _events_db(path, n: int = 5000) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ev (id INTEGER PRIMARY KEY, amt REAL, at DATETIME)")
    base = datetime(2025, 1, 1)
    conn.executemany("INSERT INTO ev VALUES (?, ?, ?)",
                     [(i, i * 1.5, (base + timedelta(hours=i)).isoformat(sep=" ")) for i in range(n)])
    conn.commit()
    conn.close()


def _extract(tmp_path, config: dict, cap: int) -> tuple[list[int], dict]:
    con = GenericSQLConnector("sqlite", {"path": "ev.db", **config}, allowed_dir=tmp_path)
    asset = con.discover()[0]
    stored = DiscoveredAsset(source_name=asset.source_name, name=asset.name, columns=asset.columns, kind="api_table")
    ids = [i for b in con.extract(stored, max_rows=cap) for i in b.column("id").to_pylist()]
    return ids, con.last_snapshot


def test_extract_records_undeclared_truncation(tmp_path) -> None:
    _events_db(tmp_path / "ev.db")
    ids, snap = _extract(tmp_path, {}, 1000)
    assert len(ids) == 1000
    assert snap["truncated"] is True and snap["rows_staged"] == 1000 and snap["row_cap"] == 1000
    assert snap["source_total_rows"] == 5000 and snap["total_rows_basis"] == "count"
    assert snap["sampling_method"] == "undeclared" and snap["representative"] is False
    ids, snap = _extract(tmp_path, {}, 5000)  # exactly the table: the extra row proves nothing was cut
    assert len(ids) == 5000 and snap["truncated"] is False and snap["representative"] is True


def test_extract_first_n_and_full(tmp_path) -> None:
    _events_db(tmp_path / "ev.db")
    _, snap = _extract(tmp_path, {"sampling": {"method": "first_n"}}, 1000)
    assert snap["sampling_method"] == "first_n" and snap["truncated"] and not snap["representative"]
    _, snap = _extract(tmp_path, {"sampling": "full"}, 10_000)
    assert snap["sampling_method"] == "full" and not snap["truncated"] and snap["rows_staged"] == 5000


def test_extract_tablesample_emulated_on_sqlite(tmp_path) -> None:
    _events_db(tmp_path / "ev.db")
    ids, snap = _extract(tmp_path, {"sampling": {"method": "tablesample", "percent": 10, "seed": 3}}, 2000)
    assert 300 < len(ids) < 700 and len(set(ids)) == len(ids)
    assert max(ids) > 4000  # spread over the whole table, not the first rows
    assert snap["truncated"] is False and snap["representative"] is True
    assert snap["sampling"] == {"method": "tablesample", "percent": 10.0, "seed": 3, "sampler": "bernoulli",
                                "native": False, "repeatable": False}
    _, cut = _extract(tmp_path, {"sampling": {"method": "tablesample", "percent": 50}}, 1000)
    assert cut["truncated"] is True and cut["representative"] is False  # sample larger than the cap


def test_extract_time_window(tmp_path) -> None:
    _events_db(tmp_path / "ev.db")
    ids, snap = _extract(tmp_path, {"sampling": {"method": "time_window", "column": "at", "window": "10d"}}, 1000)
    assert ids[0] == 4999 and ids == sorted(ids, reverse=True)  # most recent first
    assert len(ids) in (240, 241) and snap["truncated"] is False
    assert snap["population_rows"] == len(ids) and snap["source_total_rows"] == 5000
    s = snap["sampling"]
    assert (s["column"], s["window"], s["anchor"]) == ("at", "10d", "max")
    assert s["window_end"].startswith("2025-07-28T07:00") and s["window_start"].startswith("2025-07-18T07:00")
    ids, snap = _extract(tmp_path, {"sampling": {"method": "time_window", "column": "at", "window": "12w"}}, 1000)
    assert len(ids) == 1000 and snap["truncated"] is True and snap["representative"] is True
    assert snap["population_rows"] > 1000
    assert snap["sampling"]["effective_window_start"].startswith("2025-06-16")  # newest 1000 hourly rows
    with pytest.raises(InvalidInput, match="not a date or timestamp"):
        _extract(tmp_path, {"sampling": {"method": "time_window", "column": "amt", "window": "1d"}}, 10)
    with pytest.raises(InvalidInput, match="is not a column"):
        _extract(tmp_path, {"sampling": {"method": "time_window", "column": "nope", "window": "1d"}}, 10)


def test_extract_tablesample_native_duckdb_is_repeatable(tmp_path) -> None:
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("duckdb_engine")
    conn = duckdb.connect(str(tmp_path / "ev.duckdb"))
    conn.execute("CREATE TABLE ev AS SELECT range AS id, range * 2 AS v FROM range(20000)")
    conn.close()
    runs = []
    for _ in range(2):
        con = GenericSQLConnector("duckdb", {"path": "ev.duckdb", "sampling": {"method": "tablesample", "percent": 5,
                                                                                "seed": 11}}, allowed_dir=tmp_path)
        asset = con.discover()[0]
        runs.append(([i for b in con.extract(asset, max_rows=5000) for i in b.column("id").to_pylist()], con.last_snapshot))
        con.close()
    (first, snap), (second, _) = runs
    assert first == second and 600 < len(first) < 1400  # REPEATABLE (seed): the same sample twice
    assert snap["sampling"]["native"] is True and snap["sampling"]["repeatable"] is True
    assert snap["source_total_rows"] == 20000 and not snap["truncated"]


def test_csv_snapshot_truncation(tmp_path) -> None:
    pl.DataFrame({"id": list(range(3000)), "v": [1.0] * 3000}).write_csv(tmp_path / "big.csv")
    con = CSVFileConnector({"path": "big.csv", "sampling": "first_n"}, allowed_dir=tmp_path)
    asset = con.discover()[0]
    assert sum(b.num_rows for b in con.extract(asset, max_rows=1000)) == 1000
    snap = con.last_snapshot
    assert (snap["truncated"], snap["source_total_rows"], snap["sampling_method"]) == (True, 3000, "first_n")
    list(con.extract(asset, max_rows=5000))
    assert con.last_snapshot["truncated"] is False and con.last_snapshot["source_total_rows"] == 3000


def test_stage_asset_passes_the_snapshot_through_the_loader() -> None:
    class Loader:
        def load(self, source_id, asset, batches, *, workspace_id, snapshot=None):
            rows = sum(b.num_rows for b in batches)
            return {"row_count": rows, **({"snapshot": {**snapshot(), "rows_staged": rows}} if snapshot() else {})}

    class Connector:
        last_snapshot = None

        def extract(self, asset, *, max_rows):
            import pyarrow as pa

            yield pa.RecordBatch.from_pydict({"x": list(range(min(max_rows, 50)))})

    asset = DiscoveredAsset(source_name="t", name="t")
    info = stage_asset(Loader(), Connector(), "src_1", asset, config={"max_rows": 50}, platform_max=1000, workspace_id="ws_1")
    assert info["snapshot"]["truncated"] is True  # hit the cap and the connector could not say more
    assert info["snapshot"]["total_rows_basis"] == "unavailable" and "staged_at" in info["snapshot"]


# ------------------------------------------------------------------------------ caveat + check


def _snap(method: str | None, *, truncated: bool, rows: int = 1_000_000, total: int | None = 12_400_000, **params):
    spec = None if method is None else sampling.SamplingSpec(method=method, **{
        k: v for k, v in params.items() if k in sampling.SamplingSpec.model_fields})
    extra = {k: v for k, v in params.items() if k not in sampling.SamplingSpec.model_fields}
    return sampling.snapshot_record(spec=spec, rows_staged=rows, cap=1_000_000, truncated=truncated,
                                    source_total_rows=total, total_basis="count", **extra)


def test_population_caveat_texts() -> None:
    assert population_caveat(None) is None
    assert population_caveat(_snap(None, truncated=False, rows=500, total=500)) is None
    assert population_caveat(_snap("full", truncated=False, rows=500, total=500)) is None
    text = population_caveat(_snap("time_window", truncated=True, column="closed_at", window="90d",
                                   window_start="2026-06-27T00:00:00", window_end="2026-09-25T00:00:00"))
    assert text.startswith("Computed on a 1,000,000-row time-window sample (last 90 days by closed_at, "
                           "2026-06-27 to 2026-09-25) of 12.4M rows.")
    text = population_caveat(_snap("tablesample", truncated=False, rows=620_000, percent=5, sampler="bernoulli", native=True))
    assert text == "Computed on a 620,000-row 5% random sample (bernoulli) of 12.4M rows."
    text = population_caveat(_snap(None, truncated=True))
    assert "no declared sampling strategy" in text and "1,000,000 rows of 12.4M rows" in text
    assert "not a sample" in population_caveat(_snap("first_n", truncated=True))
    assert "administrator's 1,000,000-row limit" in population_caveat(_snap("full", truncated=True))


@pytest.mark.parametrize(
    ("snap", "passed", "method"),
    [
        (_snap(None, truncated=True), False, "undeclared"),
        (_snap("first_n", truncated=True), False, "first_n"),
        (_snap("full", truncated=True), False, "full"),
        (_snap("tablesample", truncated=True, percent=50), False, "tablesample"),
        (_snap(None, truncated=False, rows=10, total=10), True, "undeclared"),
        (_snap("full", truncated=False, rows=10, total=10), True, "full"),
        (_snap("first_n", truncated=False, rows=10, total=10), True, "first_n"),
        (_snap("tablesample", truncated=False, percent=5, seed=7), True, "tablesample"),
        (_snap("time_window", truncated=True, column="at", window="90d"), True, "time_window"),
    ],
)
def test_representative_population_check(snap, passed, method) -> None:
    check = population_check("staged", snap)
    assert (check["check"], check["passed"], check["method"]) == (CHECK, passed, method)
    assert check["detail"].startswith(method)
    if method == "tablesample" and passed:
        assert check["sampling"] == {"percent": 5.0, "seed": 7}


def test_representative_population_check_other_modes() -> None:
    assert population_check("pushdown", None) == {"check": CHECK, "passed": True, "method": "pushdown",
                                                  "detail": "queried in place: the whole table"}
    assert population_check("staged", None)["passed"] is True
    legacy = population_check("staged", {**_snap(None, truncated=True, total=None), "recorded": False})
    assert legacy["passed"] is False and "inferred from the staged row count" in legacy["detail"]
