"""Shared fixtures for the analytical-skills tests: a synthetic ITSM dataset with KNOWN planted
effects and data-quality defects, plus `RunSQL` implementations over in-memory DuckDB and (for
integration tests) a local Postgres.

Planted effects (see `make_incidents`):
  * SLA breach rate triples when reassignment_count >= 3 (0.12 -> 0.36)
  * resolution duration is 1.6x for incidents opened after hours (outside 08-18 or weekend)
  * duration also rises with reassignment_count (x (1 + 0.15 * count))  -> correlation
  * "Payments" holds ~35% of priority-1 incidents (20 apps, otherwise uniform)
  * monthly incident volume grows ~2.4x over 18 months
Null controls: noise_group (5 uniform groups), noise_value ~ N(50, 10), noise_flag ~ Bernoulli(0.3).
Planted DQ defects: 4 duplicate `number`s, 7 rows resolved before opened, 3 closed_at in the
future (2027), u_legacy_code ~92% NULL, constant `company`, case-variant categories
('Network'/'network'), 5 orphan assignment_group references.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any

import numpy as np
import pandas as pd

from analystos.gateway.types import QueryResult

N_INCIDENTS = 6000
NOW = dt.datetime(2026, 9, 25, 12, 0, 0)
APPS = ["Payments"] + [f"App{i:02d}" for i in range(1, 20)]
GROUP_IDS = [hashlib.md5(f"grp{i}".encode()).hexdigest() for i in range(10)]
ORPHAN_GROUP = "f" * 32


def _after_hours(ts: dt.datetime, start: int = 8, end: int = 18) -> bool:
    return ts.hour < start or ts.hour >= end or ts.weekday() >= 5


def make_incidents(n: int = N_INCIDENTS, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    months = 18
    w = 1 + 0.08 * np.arange(months)
    month = rng.choice(months, size=n, p=w / w.sum())
    start = dt.datetime(2025, 1, 1)
    opened = []
    for m in month:
        y, mo = 2025 + (m // 12), (m % 12) + 1
        first = dt.datetime(y, mo, 1)
        nxt = dt.datetime(y + (mo == 12), mo % 12 + 1, 1)
        secs = int(rng.integers(0, int((nxt - first).total_seconds())))
        opened.append(first + dt.timedelta(seconds=secs))
    assert opened[0] >= start
    after = np.array([_after_hours(t) for t in opened])
    reassign = rng.choice(7, size=n, p=[0.35, 0.25, 0.15, 0.10, 0.07, 0.05, 0.03])
    breach_p = np.where(reassign >= 3, 0.36, 0.12)
    breached = rng.random(n) < breach_p
    base = rng.lognormal(mean=np.log(8), sigma=0.8, size=n)
    dur = base * np.where(after, 1.6, 1.0) * (1 + 0.15 * reassign)
    priority = rng.choice(["1 - Critical", "2 - High", "3 - Moderate", "4 - Low"], size=n, p=[0.10, 0.20, 0.40, 0.30])
    app = []
    for p in priority:
        if p == "1 - Critical":
            app.append("Payments" if rng.random() < 0.35 else APPS[1 + int(rng.integers(0, 19))])
        else:
            app.append(APPS[int(rng.integers(0, 20))])
    category = rng.choice(["Network", "Software", "Hardware", "Database"], size=n)
    variant = rng.random(n) < 0.03
    category = np.where(variant & (category == "Network"), "network", category)
    resolved = [o + dt.timedelta(hours=float(d)) for o, d in zip(opened, dur, strict=False)]
    open_mask = rng.random(n) < 0.05
    closed = [r + dt.timedelta(hours=24) for r in resolved]
    df = pd.DataFrame({
        "sys_id": [uuid.UUID(int=int(rng.integers(0, 2**63)) << 64 | i).hex for i in range(n)],
        "number": [f"INC{i + 1:07d}" for i in range(n)],
        "opened_at": opened,
        "resolved_at": [None if m else r for r, m in zip(resolved, open_mask, strict=False)],
        "closed_at": [None if m else c for c, m in zip(closed, open_mask, strict=False)],
        "priority": priority,
        "category": category,
        "app": app,
        "assignment_group": rng.choice(GROUP_IDS, size=n),
        "reassignment_count": reassign.astype(int),
        "sla_breached": breached,
        "made_sla": np.where(breached, "false", "true"),
        "noise_group": rng.choice(list("ABCDE"), size=n),
        "noise_value": rng.normal(50, 10, size=n),
        "noise_flag": rng.random(n) < 0.3,
        "u_legacy_code": [f"L{int(x)}" if rng.random() < 0.08 else None for x in rng.integers(0, 50, size=n)],
        "company": "ACME",
    })
    # --- planted data-quality defects ---
    for i in range(4):  # duplicate numbers
        df.loc[100 + i, "number"] = df.loc[i, "number"]
    for i in range(7):  # resolved before opened
        j = 200 + i
        df.at[j, "resolved_at"] = df.loc[j, "opened_at"] - dt.timedelta(hours=5)
        df.at[j, "closed_at"] = df.loc[j, "opened_at"] + dt.timedelta(hours=1)
    for i in range(3):  # closed in the future
        df.at[300 + i, "closed_at"] = dt.datetime(2027, 3, 1 + i, 10, 0, 0)
    for i in range(5):  # orphan FK
        df.loc[400 + i, "assignment_group"] = ORPHAN_GROUP
    df["resolved_at"] = pd.to_datetime(df["resolved_at"])
    df["closed_at"] = pd.to_datetime(df["closed_at"])
    return df


def make_groups() -> pd.DataFrame:
    return pd.DataFrame({"sys_id": GROUP_IDS, "name": [f"Group {i}" for i in range(10)]})


def make_crm(seed: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    cust = pd.DataFrame({"id": np.arange(1, 201), "name": [f"Customer {i}" for i in range(1, 201)]})
    orders = pd.DataFrame({"id": np.arange(1, 1501), "customer_id": rng.integers(1, 201, size=1500),
                           "amount": rng.gamma(2.0, 50.0, size=1500).round(2),
                           "unrelated_code": rng.integers(1000, 9000, size=1500)})
    return cust, orders


def _result(sql: str, cols: list[str], rows: list[tuple], max_rows: int | None) -> QueryResult:
    truncated = False
    if max_rows is not None and len(rows) > max_rows:
        rows, truncated = rows[:max_rows], True
    fp = hashlib.sha256(sql.encode()).hexdigest()
    return QueryResult(query_id=f"q_{uuid.uuid4().hex[:12]}", columns=cols, rows=[list(r) for r in rows],
                       row_count=len(rows), truncated=truncated, fingerprint=fp,
                       result_hash=hashlib.sha256(repr(rows).encode()).hexdigest(), sql=sql)


class DuckRunSQL:
    """RunSQL over an in-memory DuckDB. Records every statement for assertions."""

    dialect = "duckdb"

    def __init__(self, con: Any):
        self.con = con
        self.calls: list[dict[str, Any]] = []

    def __call__(self, sql: str, *, purpose: str = "analysis", max_rows: int | None = None) -> QueryResult:
        self.calls.append({"sql": sql, "purpose": purpose, "max_rows": max_rows})
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        return _result(sql, cols, cur.fetchall(), max_rows)


def duck_dataset() -> DuckRunSQL:
    import duckdb

    con = duckdb.connect()
    inc = make_incidents()
    grp = make_groups()
    cust, orders = make_crm()
    con.execute("CREATE SCHEMA itsm")
    con.execute("CREATE SCHEMA crm")
    for name, df in {"itsm.incident": inc, "itsm.sys_user_group": grp, "crm.customer": cust, "crm.orders": orders}.items():
        con.register("_tmp", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _tmp")
        con.unregister("_tmp")
    return DuckRunSQL(con)


INCIDENT_COLUMNS = [
    {"name": "sys_id", "data_type": "VARCHAR", "is_key": True},
    {"name": "number", "data_type": "VARCHAR", "is_key": True},
    {"name": "opened_at", "data_type": "TIMESTAMP"},
    {"name": "resolved_at", "data_type": "TIMESTAMP"},
    {"name": "closed_at", "data_type": "TIMESTAMP"},
    {"name": "priority", "data_type": "VARCHAR"},
    {"name": "category", "data_type": "VARCHAR"},
    {"name": "app", "data_type": "VARCHAR"},
    {"name": "assignment_group", "data_type": "VARCHAR", "references": "sys_user_group"},
    {"name": "reassignment_count", "data_type": "BIGINT"},
    {"name": "sla_breached", "data_type": "BOOLEAN"},
    {"name": "made_sla", "data_type": "VARCHAR"},
    {"name": "noise_group", "data_type": "VARCHAR"},
    {"name": "noise_value", "data_type": "DOUBLE"},
    {"name": "noise_flag", "data_type": "BOOLEAN"},
    {"name": "u_legacy_code", "data_type": "VARCHAR"},
    {"name": "company", "data_type": "VARCHAR"},
]


# ---------------------------------------------------------------------------------------------
# Postgres (integration)
# ---------------------------------------------------------------------------------------------
PG_DSN = "postgresql://analystos:analystos@localhost:5432/analystos"


def pg_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(PG_DSN, connect_timeout=2):
            return True
    except Exception:  # noqa: BLE001
        return False


class PgRunSQL:
    dialect = "postgres"

    def __init__(self, conn: Any):
        self.conn = conn
        self.calls: list[dict[str, Any]] = []

    def __call__(self, sql: str, *, purpose: str = "analysis", max_rows: int | None = None) -> QueryResult:
        self.calls.append({"sql": sql, "purpose": purpose, "max_rows": max_rows})
        with self.conn.cursor() as cur:
            cur.execute(sql)
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
        return _result(sql, cols, rows, max_rows)


_PG_TYPES = {"object": "TEXT", "int64": "BIGINT", "float64": "DOUBLE PRECISION", "bool": "BOOLEAN",
             "datetime64[ns]": "TIMESTAMP", "datetime64[us]": "TIMESTAMP"}


def pg_load(conn: Any, schema: str) -> None:
    inc = make_incidents()
    grp = make_groups()
    cust, orders = make_crm()
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        for name, df in {"incident": inc, "sys_user_group": grp, "customer": cust, "orders": orders}.items():
            cols = ", ".join(f'"{c}" {_PG_TYPES.get(str(t), "TEXT")}' for c, t in df.dtypes.items())
            cur.execute(f'CREATE TABLE "{schema}"."{name}" ({cols})')
            with cur.copy(f'COPY "{schema}"."{name}" ({", ".join(chr(34) + c + chr(34) for c in df.columns)}) FROM STDIN') as cp:
                for row in df.itertuples(index=False):
                    cp.write_row([None if (v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NaT)
                                  else (v.to_pydatetime() if isinstance(v, pd.Timestamp) else
                                        (bool(v) if isinstance(v, np.bool_) else
                                         (v.item() if isinstance(v, np.generic) else v))) for v in row])
    conn.commit()


class TsqlViaDuckRunSQL:
    """Pretends to be a SQL Server source: receives T-SQL, transpiles it with sqlglot to DuckDB and
    executes it. This checks that the generated T-SQL parses AND means the same thing (date math,
    day-of-week, TOP, PERCENTILE_CONT OVER ()...) without a SQL Server instance."""

    dialect = "tsql"

    def __init__(self, duck: DuckRunSQL):
        self.duck = duck
        self.calls: list[dict[str, Any]] = []

    def __call__(self, sql: str, *, purpose: str = "analysis", max_rows: int | None = None) -> QueryResult:
        import sqlglot

        self.calls.append({"sql": sql, "purpose": purpose, "max_rows": max_rows})
        return self.duck(sqlglot.transpile(sql, read="tsql", write="duckdb")[0], purpose=purpose, max_rows=max_rows)


ASSET_COLUMNS = {
    "itsm.incident": [c["name"] for c in INCIDENT_COLUMNS],
    "itsm.sys_user_group": ["sys_id", "name"],
    "crm.customer": ["id", "name"],
    "crm.orders": ["id", "customer_id", "amount", "unrelated_code"],
}


def gateway_scope(dialect: str, schema_map: dict[str, str] | None = None) -> Any:
    """DataScope over the fixture assets (optionally renaming schemas, e.g. for a temp Postgres schema)."""
    from analystos.contracts.policy import DataScope

    ren = schema_map or {}

    def name(a: str) -> str:
        s, t = a.split(".")
        return f"{ren.get(s, s)}.{t}"

    assets = {name(a): cols for a, cols in ASSET_COLUMNS.items()}
    return DataScope(workspace_id="ws", user_id="u", role="analyst", source_ids=["src"], assets=list(assets),
                     asset_sources={a: "src" for a in assets}, columns=assets, source_dialects={"src": dialect})


class GatewayRunSQL:
    """Routes every statement through the real gateway validator (analystos.gateway.validator) and
    executes the *re-generated, row-capped* SQL it returns, exactly like production."""

    def __init__(self, inner: Any, scope: Any):
        self.inner = inner
        self.scope = scope
        self.dialect = inner.dialect
        self.calls: list[dict[str, Any]] = []

    def __call__(self, sql: str, *, purpose: str = "analysis", max_rows: int | None = None) -> QueryResult:
        from analystos.gateway.validator import validate_sql

        cap = max_rows or self.scope.max_rows
        v = validate_sql(self.scope, sql, max_rows=cap)
        self.calls.append({"sql": sql, "executable_sql": v.executable_sql, "purpose": purpose})
        res = self.inner(v.executable_sql, purpose=purpose, max_rows=cap)
        return res
