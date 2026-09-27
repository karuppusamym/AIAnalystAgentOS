"""P6-02 watermark incremental staging (ADR-0016) on in-memory doubles: the loader double commits rows
and state together or not at all (like one Postgres transaction), the connector double pages a table
that can change between pages. The same behaviour against the real loader and the ServiceNow mock is in
tests/integration/test_pipelines.py."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta

import pyarrow as pa
import pytest

from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn
from analystos.contracts.recipe import SOFT_DELETE_COLUMN, Incremental, parse_duration
from analystos.core.errors import InvalidInput
from analystos.staging.incremental import dedupe, stage_incremental

T0 = datetime(2026, 1, 1)
SCHEMA = pa.schema([pa.field("id", pa.string()), pa.field("v", pa.int64()), pa.field("updated", pa.timestamp("us"))])
ASSET = DiscoveredAsset(source_name="t", name="t", columns=[DiscoveredColumn(name=c, data_type="text") for c in SCHEMA.names])


class Source:
    """A table behind a paged API. `on_page` runs after each page (a concurrent writer)."""

    page_size = 3

    def __init__(self, n: int = 20) -> None:
        self.rows = {f"k{i:03d}": {"id": f"k{i:03d}", "v": i, "updated": T0 + timedelta(minutes=i)} for i in range(n)}
        self.on_page = None
        self.requests = 0

    def high_watermark(self, asset, column):
        return max((r[column] for r in self.rows.values()), default=None)

    def _batch(self, rows):
        return pa.RecordBatch.from_arrays([pa.array([r[c] for r in rows], type=SCHEMA.field(c).type) for c in SCHEMA.names],
                                          schema=SCHEMA)

    def extract_window(self, asset, *, watermark, since, until, key, max_rows):
        after, fetched = None, 0
        while fetched < max_rows + 1:
            page = sorted((r for r in self.rows.values()
                           if (since is None or r[watermark] >= since) and (until is None or r[watermark] <= until)
                           and (after is None or r[key] > after)), key=lambda r: r[key])[:self.page_size]
            self.requests += 1
            if not page:
                return
            yield self._batch([dict(r) for r in page])
            fetched += len(page)
            after = page[-1][key]
            if self.on_page:
                self.on_page(self)

    def extract_keys(self, asset, *, keys, max_rows):
        ids = sorted(self.rows)
        yield pa.RecordBatch.from_arrays([pa.array(ids, type=pa.string())], names=["id"])

    def naive_offset_window(self, since):
        """The dlt spike's first attempt: offset paging against `watermark >= since ORDER BY watermark`
        while the filter's result set moves."""
        out, offset = [], 0
        while True:
            page = sorted((r for r in self.rows.values() if r["updated"] >= since),
                          key=lambda r: (r["updated"], r["id"]))[offset:offset + self.page_size]
            if not page:
                return out
            out += [dict(r) for r in page]
            offset += len(page)
            if self.on_page:
                self.on_page(self)


class Crash(Exception):
    pass


class Loader:
    """Rows and load state commit together (one transaction), or neither does."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple, dict]] = {}
        self.state: dict[str, dict] = {}
        self.columns: dict[str, list[str]] = {}
        self.crash_before_commit = False

    def _names(self, source_id, table):
        return "src_x", table

    def read_state(self, source_id, table):
        return copy.deepcopy(self.state.get(table)) if table in self.tables else None

    def _commit(self, table, rows, state_fn, columns=None):
        new_state = state_fn(copy.deepcopy(self.state.get(table))) if state_fn else None
        if self.crash_before_commit:
            raise Crash("process killed before COMMIT")
        self.tables[table] = rows
        if columns:
            self.columns[table] = columns
        if new_state is not None:
            self.state[table] = new_state
        return new_state

    def _info(self, table, state):
        rows = self.tables[table]
        return {"schema": "src_x", "table": table, "row_count": len(rows), "content_fingerprint": fingerprint(rows),
                "columns": [], "state": state}

    def load(self, source_id, asset, batches, *, workspace_id, mode="replace", keys=None, fingerprint="stream", state=None):
        name = asset.name
        incoming = [r for b in batches for r in b.to_pylist()]
        keys = keys or ["id"]
        current = {} if mode == "replace" else dict(self.tables.get(name, {}))
        for r in incoming:
            current[tuple(r[k] for k in keys)] = r
        st = self._commit(name, current, state, list(incoming[0]) if incoming else None)
        return {**self._info(name, st), "rows_loaded": len(incoming)}

    def table_info(self, source_id, table):
        return self._info(table, None)

    def save_state(self, source_id, table, state):
        return self._commit(table, self.tables[table], state)

    def reconcile(self, source_id, table, key_batches, *, keys, policy, workspace_id, max_delete_pct=50.0, state=None):
        live = {tuple([v]) for b in key_batches for v in b.column(0).to_pylist()}
        rows = dict(self.tables[table])
        gone = [k for k, r in rows.items() if k not in live and not r.get(SOFT_DELETE_COLUMN)]
        if rows and gone and policy != "ignore" and 100 * len(gone) / len(rows) > max_delete_pct:
            raise InvalidInput("the key read looks partial")
        for k in gone:
            if policy == "reconcile":
                del rows[k]
            elif policy == "soft":
                rows[k] = {**rows[k], SOFT_DELETE_COLUMN: "deleted"}
        st = self._commit(table, rows, state)
        return {"policy": policy, "keys_received": len(live), "missing_rows": len(gone),
                "deleted_rows": len(gone) if policy == "reconcile" else 0,
                "soft_deleted_rows": len(gone) if policy == "soft" else 0, **self._info(table, st)}


def fingerprint(rows: dict) -> frozenset:
    return frozenset((r["id"], r["v"], r["updated"]) for r in rows.values())


def reference(src: Source) -> frozenset:
    return frozenset((r["id"], r["v"], r["updated"]) for r in src.rows.values())


INC = Incremental(watermark="updated", key=["id"], late_window="5m", deletes="reconcile")


def stage(loader, src, inc=INC, **kw):
    return stage_incremental(loader, src, "src_1", ASSET, inc, workspace_id="ws", cap=kw.pop("cap", 1000),
                             now=kw.pop("now", T0 + timedelta(days=1)), **kw)


def touch(src: Source, key: str, minutes: int, v: int | None = None) -> None:
    r = src.rows[key]
    r["updated"] = T0 + timedelta(minutes=minutes)
    if v is not None:
        r["v"] = v


def test_initial_load_then_windows_equal_a_full_rebuild():
    src, loader = Source(), Loader()
    first = stage(loader, src)
    assert first["incremental"]["load_mode"] == "replace" and loader.state["t"]["watermark"] == (T0 + timedelta(minutes=19)).isoformat()
    touch(src, "k003", 100, v=333)
    src.rows["k900"] = {"id": "k900", "v": 900, "updated": T0 + timedelta(minutes=101)}
    out = stage(loader, src)
    run = out["incremental"]
    assert run["load_mode"] == "merge" and run["rows_fetched"] == 8  # k014..k019 (late window) + k003 + k900
    assert run["since"] == (T0 + timedelta(minutes=14)).isoformat()
    assert fingerprint(loader.tables["t"]) == reference(src)
    assert out["snapshot"]["incremental"]["watermark"] == (T0 + timedelta(minutes=101)).isoformat()
    assert out["snapshot"]["truncated"] is False and out["snapshot"]["content_fingerprint"]


def test_fixed_cursor_keyset_pagination_does_not_skip_rows_the_offset_trap_does():
    src = Source(30)
    moved = iter(range(30))

    def writer(s: Source) -> None:  # every page, one not-yet-read row is updated (moves to the end of the filter)
        for key in sorted(s.rows, reverse=True):
            if key in seen_by_writer:
                continue
            seen_by_writer.add(key)
            touch(s, key, 1000 + next(moved))
            return

    seen_by_writer: set[str] = set()
    src.on_page = writer
    naive = src.naive_offset_window(T0)
    assert len({r["id"] for r in naive}) < 30  # the trap is real: offset paging on a moving filter skips rows

    src2, loader = Source(30), Loader()
    seen_by_writer.clear()
    moved = iter(range(30))
    src2.on_page = writer
    stage(loader, src2)  # rows updated mid-read leave the window upward; the next window picks them up
    src2.on_page = None
    stage(loader, src2)
    assert fingerprint(loader.tables["t"]) == reference(src2) and len(loader.tables["t"]) == 30


def test_a_crash_between_load_and_watermark_re_reads_the_window():
    src, loader = Source(), Loader()
    stage(loader, src)
    before_rows, before_state = copy.deepcopy(loader.tables["t"]), copy.deepcopy(loader.state["t"])
    touch(src, "k005", 200, v=555)
    loader.crash_before_commit = True
    with pytest.raises(Crash):
        stage(loader, src)
    assert loader.tables["t"] == before_rows and loader.state["t"] == before_state  # neither moved
    loader.crash_before_commit = False
    out = stage(loader, src)
    assert out["incremental"]["since"] == (T0 + timedelta(minutes=14)).isoformat()  # the same window again
    assert fingerprint(loader.tables["t"]) == reference(src)


def test_retries_and_overlapping_windows_do_not_double_count_or_move_the_watermark_back():
    src, loader = Source(), Loader()
    stage(loader, src)
    touch(src, "k001", 50, v=111)
    stage(loader, src)
    once = fingerprint(loader.tables["t"])
    wm = loader.state["t"]["watermark"]
    stage(loader, src)  # a retry of the same window
    stage(loader, src, mode="replay", window=(T0, T0 + timedelta(minutes=60)))  # an overlapping older window
    assert fingerprint(loader.tables["t"]) == once and len(loader.tables["t"]) == 20
    assert loader.state["t"]["watermark"] == wm

    # a run whose cursor is older than the stored watermark (an overlapping schedule that started earlier)
    stale = Source()
    stale.rows = copy.deepcopy(src.rows)
    stale.rows["k001"]["updated"] = T0 + timedelta(minutes=40)
    stage(loader, stale)
    assert loader.state["t"]["watermark"] == wm


def test_duplicates_in_a_window_keep_the_newest_watermark():
    rows = [["a", 1, T0], ["a", 2, T0 + timedelta(seconds=5)], ["b", 3, T0], ["a", 9, T0 + timedelta(seconds=1)]]
    kept, dropped = dedupe(SCHEMA, rows, ["id"], "updated")
    assert dropped == 2 and sorted(kept) == [["a", 2, T0 + timedelta(seconds=5)], ["b", 3, T0]]
    with pytest.raises(InvalidInput, match="null"):
        dedupe(SCHEMA, [[None, 1, T0]], ["id"], "updated")


def test_late_rows_inside_the_late_window_are_picked_up():
    src, loader = Source(), Loader()
    stage(loader, src)
    src.rows["late1"] = {"id": "late1", "v": 1, "updated": T0 + timedelta(minutes=16)}  # 3 minutes behind the watermark
    src.rows["late2"] = {"id": "late2", "v": 2, "updated": T0 + timedelta(minutes=2)}  # far outside the window
    stage(loader, src)
    assert ("late1",) in loader.tables["t"] and ("late2",) not in loader.tables["t"]
    stage(loader, src, mode="backfill", window=(T0, T0 + timedelta(minutes=10)))  # a backfill range catches it
    assert fingerprint(loader.tables["t"]) == reference(src)


@pytest.mark.parametrize("policy", ["reconcile", "soft", "ignore"])
def test_deletes_are_handled_by_the_full_reconcile(policy):
    src, loader = Source(), Loader()
    inc = INC.model_copy(update={"deletes": policy})
    stage(loader, src, inc=inc)
    del src.rows["k007"]
    stage(loader, src, inc=inc)
    assert ("k007",) in loader.tables["t"]  # a watermark cannot see a delete
    out = stage(loader, src, inc=inc, mode="reconcile")
    row = loader.tables["t"].get(("k007",))
    if policy == "reconcile":
        assert row is None and out["incremental"]["reconcile"]["deleted_rows"] == 1
        assert fingerprint(loader.tables["t"]) == reference(src)
    elif policy == "soft":
        assert row[SOFT_DELETE_COLUMN] and out["incremental"]["reconcile"]["soft_deleted_rows"] == 1
    else:
        assert row is not None and out["incremental"]["reconcile"]["missing_rows"] == 1


def test_scheduled_reconcile_runs_when_due_and_an_empty_key_read_is_refused():
    src, loader = Source(), Loader()
    stage(loader, src, now=T0)
    del src.rows["k002"]
    stage(loader, src, now=T0 + timedelta(hours=1))
    assert ("k002",) in loader.tables["t"]
    out = stage(loader, src, now=T0 + timedelta(days=8))  # a week later: the weekly reconcile is due
    assert out["incremental"]["reconcile"]["deleted_rows"] == 1 and ("k002",) not in loader.tables["t"]
    src.rows.clear()  # an outage that answers with no keys
    with pytest.raises(InvalidInput, match="partial"):
        stage(loader, src, mode="reconcile")


def test_a_window_over_the_cap_is_refused_without_moving_the_watermark():
    src, loader = Source(), Loader()
    stage(loader, src)
    wm = loader.state["t"]["watermark"]
    for i in range(10):
        touch(src, f"k{i:03d}", 500 + i)
    with pytest.raises(InvalidInput, match="more than 5 rows"):
        stage(loader, src, cap=5)
    assert loader.state["t"]["watermark"] == wm


def test_replay_needs_state_and_a_range():
    src, loader = Source(), Loader()
    with pytest.raises(InvalidInput, match="initial load"):
        stage(loader, src, mode="replay", window=(T0, T0))
    stage(loader, src)
    with pytest.raises(InvalidInput, match="range"):
        stage(loader, src, mode="replay")


def test_durations():
    assert [parse_duration(v) for v in (90, "90", "15m", "2h", "1d", "PT15M", "P1DT2H", "PT30S")] == \
        [90, 90, 900, 7200, 86400, 900, 93600, 30]
    for bad in ("soon", "-5", "P", True):
        with pytest.raises(ValueError):
            parse_duration(bad)
