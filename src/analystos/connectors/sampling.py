"""Declared sampling strategy for staged snapshots (P4-C12, review C8).

A statistical claim is about the population that was sampled, and an unordered ``LIMIT`` is not a
sample of anything defined. So a staged source declares how its snapshot relates to the origin
table (``config.sampling``), and every snapshot records what it actually is:

* ``full``         every row, bounded only by the administrator's hard limit (``staged_max_rows``);
                   when the table is larger the snapshot is marked truncated;
* ``time_window``  the most recent ``window`` by a declared timestamp ``column`` (``WHERE column >=
                   cutoff ORDER BY column DESC``), anchored on the column's maximum (or ``now``);
* ``tablesample``  the engine's native random sample (``TABLESAMPLE BERNOULLI/SYSTEM (p)`` with a
                   seed where the engine has one); engines without one get an independent per-row
                   random predicate, which is the same Bernoulli design, recorded as emulated;
* ``first_n``      the first rows the engine returns. Allowed only when declared, and recorded as
                   non-representative whenever the table is larger than the cap.

Config shape (per source, optionally overridden per asset by origin or staged name)::

    sampling: {method: tablesample, percent: 5, seed: 7,
               assets: {"sales.orders": {method: time_window, column: placed_at, window: 90d}}}
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from analystos.core.errors import InvalidInput

Method = Literal["full", "time_window", "tablesample", "first_n"]
UNDECLARED = "undeclared"
SQL_METHODS = frozenset({"full", "time_window", "tablesample", "first_n"})
FILE_API_METHODS = frozenset({"full", "first_n"})  # ServiceNow and files have no engine-side sampling

_WINDOW = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.I)
_UNIT_HOURS = {"h": 1, "d": 24, "w": 24 * 7}
_UNIT_WORD = {"h": "hour", "d": "day", "w": "week"}


class SamplingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Method
    column: str | None = None  # time_window: timestamp/date column (origin or staged name)
    window: str | None = None  # time_window: "<n>h" | "<n>d" | "<n>w"
    anchor: Literal["max", "now"] = "max"  # time_window: window ends at MAX(column) or now
    percent: float | None = Field(None, gt=0, lt=100)  # tablesample
    sampler: Literal["bernoulli", "system"] | None = None  # tablesample: None = the engine's row-level default
    seed: int | None = Field(None, ge=0, le=2_147_483_647)  # tablesample: None = derived from the table name
    count: Literal["exact", "estimate"] = "exact"  # how source_total_rows is measured

    @model_validator(mode="after")
    def _shape(self) -> SamplingSpec:
        if self.method == "time_window":
            if not self.column or not self.window:
                raise ValueError("time_window needs 'column' (a timestamp or date column) and 'window' (e.g. 90d)")
            window_delta(self.window)
        elif self.column or self.window:
            raise ValueError("'column' and 'window' apply only to method time_window")
        if self.method == "tablesample":
            if self.percent is None:
                raise ValueError("tablesample needs 'percent' (0 < percent < 100; use method full for 100%)")
        elif self.percent is not None or self.sampler is not None or self.seed is not None:
            raise ValueError("'percent', 'sampler' and 'seed' apply only to method tablesample")
        return self


def window_delta(window: str) -> timedelta:
    m = _WINDOW.match(str(window))
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"window {window!r} must look like 48h, 90d or 12w")
    return timedelta(hours=int(m.group(1)) * _UNIT_HOURS[m.group(2).lower()])


def window_label(window: str) -> str:
    m = _WINDOW.match(str(window))
    if not m:
        return str(window)
    n, unit = int(m.group(1)), _UNIT_WORD[m.group(2).lower()]
    return f"{n} {unit}{'s' if n != 1 else ''}"


def _parse(raw: Any, where: str) -> SamplingSpec:
    if isinstance(raw, str):
        raw = {"method": raw}
    if not isinstance(raw, dict):
        raise InvalidInput(f"{where} must be a mapping such as {{method: tablesample, percent: 5}}")
    try:
        return SamplingSpec.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc") or ())
        msg = str(first.get("msg", "invalid")).removeprefix("Value error, ")
        raise InvalidInput(f"{where}{'.' + loc if loc else ''}: {msg}") from None


def sampling_for(config: dict[str, Any] | None, *names: str | None) -> SamplingSpec | None:
    """The declared strategy for one asset: a per-asset override (matched case-insensitively on any
    of ``names``) merged over the source default. None means undeclared."""
    raw = (config or {}).get("sampling")
    if raw in (None, "", {}):
        return None
    if isinstance(raw, str):
        raw = {"method": raw}
    if not isinstance(raw, dict):
        raise InvalidInput("config.sampling must be a mapping")
    base = {k: v for k, v in raw.items() if k != "assets"}
    overrides = {str(k).lower(): v for k, v in (raw.get("assets") or {}).items()}
    for name in names:
        if name and str(name).lower() in overrides:
            over = overrides[str(name).lower()]
            over = {"method": over} if isinstance(over, str) else dict(over or {})
            merged = over if "method" in over and over["method"] != base.get("method") else {**base, **over}
            return _parse(merged, f"config.sampling.assets.{name}")
    if not base:
        return None
    return _parse(base, "config.sampling")


def validate_sampling(kind: str, is_sql: bool, config: dict[str, Any] | None, dialect: str | None = None) -> None:
    """Registration-time check of ``config.sampling`` (and every per-asset override) for this kind."""
    raw = (config or {}).get("sampling")
    if raw in (None, "", {}):
        return
    overrides = raw.get("assets") or {} if isinstance(raw, dict) else {}
    if not isinstance(overrides, dict):
        raise InvalidInput("config.sampling.assets must map an asset name to its sampling strategy")
    specs = [sampling_for(config)] + [sampling_for(config, name) for name in overrides]
    allowed = SQL_METHODS if is_sql else FILE_API_METHODS
    for spec in specs:
        if spec is None:
            continue
        if spec.method not in allowed:
            raise InvalidInput(f"sampling method '{spec.method}' is not available for {kind} sources "
                               f"(allowed: {', '.join(sorted(allowed))})")
        if spec.method == "tablesample" and dialect:
            tablesample_sql(dialect, spec.percent or 1, spec.sampler, seed=0)


def row_cap(config: dict[str, Any] | None, platform_max: int, spec: SamplingSpec | None) -> int:
    """``full`` is bounded only by the administrator's hard limit; everything else may lower it."""
    if spec is not None and spec.method == "full":
        return int(platform_max)
    return min(int((config or {}).get("max_rows") or platform_max), int(platform_max))


def default_seed(name: str) -> int:
    """A stable per-table seed so a sample is repeatable without the user choosing one."""
    return zlib.crc32(name.encode()) & 0x7FFFFFFF


# ------------------------------------------------------------------------------ SQL per dialect


@dataclass(frozen=True)
class TableSampleSQL:
    from_suffix: str | None  # appended after the table reference in FROM
    where: str | None  # an emulated per-row random predicate, when the engine has no TABLESAMPLE
    sampler: str
    native: bool
    repeatable: bool


def _num(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


# dialect -> (samplers the native clause supports, default sampler)
_NATIVE: dict[str, tuple[frozenset[str], str]] = {
    "postgres": (frozenset({"bernoulli", "system"}), "bernoulli"),
    "duckdb": (frozenset({"bernoulli", "system"}), "bernoulli"),
    "snowflake": (frozenset({"bernoulli", "system"}), "bernoulli"),
    "trino": (frozenset({"bernoulli", "system"}), "bernoulli"),
    "oracle": (frozenset({"bernoulli", "system"}), "bernoulli"),
    "databricks": (frozenset({"bernoulli"}), "bernoulli"),
    "tsql": (frozenset({"system"}), "system"),
    "bigquery": (frozenset({"system"}), "system"),
}
# dialects without TABLESAMPLE: an independent per-row random predicate (a Bernoulli sample)
_EMULATED = frozenset({"mysql", "sqlite", "redshift", "clickhouse"})


def tablesample_sql(dialect: str, percent: float, sampler: str | None, *, seed: int) -> TableSampleSQL:
    """The sampling clause for ``dialect``. Only validated numbers are interpolated."""
    p = float(percent)
    if not 0 < p < 100:
        raise InvalidInput("tablesample percent must be greater than 0 and less than 100")
    seed = int(seed)
    ps = _num(p)
    if dialect in _NATIVE:
        supported, default = _NATIVE[dialect]
        chosen = sampler or default
        if chosen not in supported:
            raise InvalidInput(f"{dialect} supports only sampler: {', '.join(sorted(supported))} for tablesample")
        up = chosen.upper()
        if dialect in ("postgres",):
            return TableSampleSQL(f"TABLESAMPLE {up} ({ps}) REPEATABLE ({seed})", None, chosen, True, True)
        if dialect == "duckdb":
            return TableSampleSQL(f"TABLESAMPLE {up} ({ps} PERCENT) REPEATABLE ({seed})", None, chosen, True, True)
        if dialect == "snowflake":
            return TableSampleSQL(f"TABLESAMPLE {up} ({ps}) SEED ({seed})", None, chosen, True, True)
        if dialect == "trino":
            return TableSampleSQL(f"TABLESAMPLE {up} ({ps})", None, chosen, True, False)
        if dialect == "oracle":
            block = " BLOCK" if chosen == "system" else ""
            return TableSampleSQL(f"SAMPLE{block} ({ps}) SEED ({seed})", None, chosen, True, True)
        if dialect == "databricks":
            return TableSampleSQL(f"TABLESAMPLE ({ps} PERCENT) REPEATABLE ({seed})", None, chosen, True, True)
        if dialect == "tsql":
            return TableSampleSQL(f"TABLESAMPLE SYSTEM ({ps} PERCENT) REPEATABLE ({seed})", None, chosen, True, True)
        return TableSampleSQL(f"TABLESAMPLE SYSTEM ({ps} PERCENT)", None, chosen, True, False)  # bigquery
    if dialect in _EMULATED:
        if sampler == "system":
            raise InvalidInput(f"{dialect} has no block sampling; use sampler: bernoulli (emulated per row)")
        fraction = _num(p / 100)
        per_million = max(1, int(round(p * 10_000)))
        where = {
            "mysql": (f"RAND({seed}) < {fraction}", True),
            "redshift": (f"RANDOM() < {fraction}", False),
            "clickhouse": (f"randCanonical() < {fraction}", False),
            "sqlite": (f"(ABS(RANDOM()) % 1000000) < {per_million}", False),
        }[dialect]
        return TableSampleSQL(None, where[0], "bernoulli", False, where[1])
    raise InvalidInput(f"tablesample is not supported for the {dialect} dialect; declare time_window or full")


# ------------------------------------------------------------------------------ snapshot record


def snapshot_record(*, spec: SamplingSpec | None, rows_staged: int, cap: int, truncated: bool,
                    source_total_rows: int | None, total_basis: str, population_rows: int | None = None,
                    **params: Any) -> dict[str, Any]:
    """What a staged snapshot is, relative to its origin table. Stored on the asset, returned by the
    loader and emitted with ``metadata.collected``; findings and verification read it.

    ``representative``: a time window stays a defined population when capped (the most recent rows);
    every other method describes its population only when the cap was not hit."""
    method = spec.method if spec is not None else UNDECLARED
    sampling: dict[str, Any] = {"method": method}
    if spec is not None:
        drop = {"method", "count"} | (set() if method == "time_window" else {"anchor"})
        sampling.update({k: v for k, v in spec.model_dump(exclude=drop).items() if v is not None})
    sampling.update({k: v for k, v in params.items() if v is not None})
    return {
        "rows_staged": int(rows_staged),
        "source_total_rows": None if source_total_rows is None else int(source_total_rows),
        "total_rows_basis": total_basis,
        "population_rows": None if population_rows is None else int(population_rows),
        "row_cap": int(cap),
        "truncated": bool(truncated),
        "sampling_method": method,
        "sampling": sampling,
        "representative": method == "time_window" or not truncated,
    }
