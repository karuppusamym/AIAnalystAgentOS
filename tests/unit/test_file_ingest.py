"""P6-06 file ingestion, the parts that need no database: reading every format and the mapping
contract. Load modes, idempotent merge and fingerprints run on Postgres in
`tests/integration/test_file_ingest_loads.py`."""
from __future__ import annotations

import json

import polars as pl
import pytest

from analystos.core.errors import InvalidInput
from analystos.services.file_ingest import ColumnMapping, IngestSpec, apply_mapping, detect_format, read_file

ROWS = [{"Order ID": 1, "Customer": "Ada", "Amount": "12.50", "Ordered": "2024-04-01"},
        {"Order ID": 2, "Customer": "Bo", "Amount": "7.25", "Ordered": "2024-04-02"}]


@pytest.fixture()
def files(tmp_path):
    df = pl.DataFrame(ROWS)
    out = {}
    out["csv"] = tmp_path / "orders.csv"
    df.write_csv(out["csv"])
    out["json"] = tmp_path / "orders.json"
    out["json"].write_text(json.dumps(ROWS))
    out["ndjson"] = tmp_path / "orders.ndjson"
    out["ndjson"].write_text("\n".join(json.dumps(r) for r in ROWS) + "\n")
    out["parquet"] = tmp_path / "orders.parquet"
    df.write_parquet(out["parquet"])
    pytest.importorskip("openpyxl")
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "orders"
    ws.append(list(ROWS[0]))
    for r in ROWS:
        ws.append(list(r.values()))
    out["excel"] = tmp_path / "orders.xlsx"
    wb.save(out["excel"])
    return out


@pytest.mark.parametrize("kind", ["csv", "json", "ndjson", "parquet", "excel"])
def test_every_format_reads_the_same_rows(files, kind):
    path = files[kind]
    df = read_file(path, detect_format(path), sheet="orders" if kind == "excel" else None)
    assert df.columns == ["Order ID", "Customer", "Amount", "Ordered"]
    assert df.height == 2 and df.get_column("Customer").to_list() == ["Ada", "Bo"]


def test_a_json_file_must_be_an_array_or_ndjson(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text('"just a string"')
    with pytest.raises(InvalidInput, match="neither a JSON array"):
        read_file(bad, "json")
    with pytest.raises(InvalidInput, match="cannot tell the format"):
        detect_format(tmp_path / "x.bin")


def test_the_default_mapping_sanitises_every_column(files):
    df, applied = apply_mapping(read_file(files["csv"], "csv"), None)
    assert df.columns == ["order_id", "customer", "amount", "ordered"]
    assert [a["source"] for a in applied] == ["Order ID", "Customer", "Amount", "Ordered"]


def test_a_mapped_source_column_must_exist_and_is_named(files):
    with pytest.raises(InvalidInput) as err:
        apply_mapping(read_file(files["csv"], "csv"), [ColumnMapping(source="Order ID"), ColumnMapping(source="Price")])
    assert "'Price'" in err.value.message and err.value.details["columns"] == ["Price"]


def test_target_names_must_be_unique_after_sanitising(files):
    df = read_file(files["csv"], "csv").with_columns(pl.col("Order ID").alias("order_id"))
    with pytest.raises(InvalidInput) as err:
        apply_mapping(df, None)
    assert "'Order ID' and 'order_id' both become 'order_id'" in err.value.message
    with pytest.raises(InvalidInput, match="both become 'id'"):
        apply_mapping(df, [ColumnMapping(source="Order ID", target="id"), ColumnMapping(source="Customer", target="ID")])


def test_declared_types_convert_or_refuse_with_the_column_and_row_never_null(files):
    df = read_file(files["csv"], "csv")
    mapped, _ = apply_mapping(df, [ColumnMapping(source="Order ID", target="order_id", type="bigint"),
                                   ColumnMapping(source="Amount", type="numeric(10,2)"),
                                   ColumnMapping(source="Ordered", type="date")])
    assert mapped.schema["order_id"] == pl.Int64 and mapped.schema["ordered"] == pl.Date
    assert str(mapped.get_column("amount").to_list()[0]) == "12.50"
    broken = df.with_columns(pl.Series("Amount", ["12.50", "n/a"]))
    with pytest.raises(InvalidInput) as err:
        apply_mapping(broken, [ColumnMapping(source="Amount", type="double")])
    assert err.value.details == {"column": "Amount", "row": 2}
    dates = df.with_columns(pl.Series("Ordered", ["2024-04-01", "31/04/2024"]))
    with pytest.raises(InvalidInput, match="'Ordered' does not convert to date \\(first at row 2\\)"):
        apply_mapping(dates, [ColumnMapping(source="Ordered", type="date")])


def test_the_ingest_spec_is_a_closed_contract():
    spec = IngestSpec(path="orders.csv", table="orders", mode="merge", keys=["order_id"])
    assert spec.mode == "merge" and spec.mapping is None
    with pytest.raises(ValueError):
        IngestSpec(path="orders.csv", table="orders", mode="upsert")
