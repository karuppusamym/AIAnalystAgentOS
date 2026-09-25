"""DashboardSpec.layout -> Superset ``position_json`` (dashboard layout v2).

Structure::

    ROOT_ID (ROOT) -> GRID_ID (GRID) -> ROW-n (ROW) -> CHART-<key> / MARKDOWN-summary
    HEADER_ID (HEADER, meta.text = title)

Superset's grid is 12 columns wide; a row's children widths must sum to <= 12. Heights are in
grid units (1 unit = 8px; Superset's default chart height is 50). Ids are deterministic so an
update PUTs the same layout keys (stable diffs, idempotent replays).
"""
from __future__ import annotations

import re
from typing import Any

from analystos.contracts.bi import DashboardSpec

GRID_COLUMNS = 12
DEFAULT_HEIGHT = 50
_DEFAULT_WIDTH = {"kpi": 3, "table": 12}
_DEFAULT_HEIGHTS = {"kpi": 28, "table": 60}


def grid_width(width: Any, chart_type: str | None) -> int:
    if isinstance(width, int) and width > 0:
        return min(width, GRID_COLUMNS)
    return _DEFAULT_WIDTH.get(chart_type or "", 6)


def grid_height(height: Any, chart_type: str | None) -> int:
    """Spec heights < 20 are treated as coarse row units (x12 grid units); larger values are grid units."""
    if isinstance(height, (int, float)) and height > 0:
        h = int(height)
        return max(16, min(h * 12, 120)) if h < 20 else min(h, 200)
    return _DEFAULT_HEIGHTS.get(chart_type or "", DEFAULT_HEIGHT)


def _node_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", key)


def layout_rows(dashboard: DashboardSpec, chart_types: dict[str, str]) -> list[list[tuple[str, int, int]]]:
    """Rows of (chart_key, width, height), every dashboard chart exactly once, each row width <= 12."""
    placed: set[str] = set()
    by_row: dict[int, list[tuple[int, int, str, Any, Any]]] = {}
    for order, cell in enumerate(dashboard.layout):
        key = str(cell.get("chart"))
        if key not in dashboard.charts or key in placed:
            continue
        placed.add(key)
        row = cell.get("row")
        row = int(row) if isinstance(row, (int, float)) else 1000 + order
        col = cell.get("col")
        col = int(col) if isinstance(col, (int, float)) else order
        by_row.setdefault(row, []).append((col, order, key, cell.get("width"), cell.get("height")))

    requested: list[list[tuple[str, int, int]]] = []
    for row in sorted(by_row):
        cells = sorted(by_row[row])
        requested.append(
            [(k, grid_width(w, chart_types.get(k)), grid_height(h, chart_types.get(k))) for _, _, k, w, h in cells]
        )
    leftovers = [k for k in dashboard.charts if k not in placed]
    if leftovers:
        requested.append(
            [(k, grid_width(None, chart_types.get(k)), grid_height(None, chart_types.get(k))) for k in leftovers]
        )

    rows: list[list[tuple[str, int, int]]] = []
    for req in requested:  # spill anything past 12 columns onto a new row
        current: list[tuple[str, int, int]] = []
        used = 0
        for item in req:
            if current and used + item[1] > GRID_COLUMNS:
                rows.append(current)
                current, used = [], 0
            current.append(item)
            used += item[1]
        if current:
            rows.append(current)
    return rows


def build_position_json(
    dashboard: DashboardSpec,
    chart_ids: dict[str, int],
    *,
    chart_types: dict[str, str] | None = None,
    slice_names: dict[str, str] | None = None,
    chart_titles: dict[str, str] | None = None,
) -> dict[str, Any]:
    chart_types = chart_types or {}
    slice_names = slice_names or {}
    chart_titles = chart_titles or {}
    pos: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [], "parents": ["ROOT_ID"]},
        "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID", "meta": {"text": dashboard.title}},
    }
    row_n = 0

    def new_row() -> str:
        nonlocal row_n
        row_n += 1
        rid = f"ROW-{row_n}"
        pos[rid] = {
            "type": "ROW",
            "id": rid,
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        }
        pos["GRID_ID"]["children"].append(rid)
        return rid

    if dashboard.summary_markdown.strip():
        rid = new_row()
        mid = "MARKDOWN-summary"
        pos[mid] = {
            "type": "MARKDOWN",
            "id": mid,
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID", rid],
            "meta": {"width": GRID_COLUMNS, "height": 26, "code": dashboard.summary_markdown},
        }
        pos[rid]["children"].append(mid)

    for row in layout_rows(dashboard, chart_types):
        rid = new_row()
        for key, width, height in row:
            cid = f"CHART-{_node_key(key)}"
            meta: dict[str, Any] = {"width": width, "height": height, "chartId": chart_ids[key]}
            if key in slice_names:
                meta["sliceName"] = slice_names[key]
            if key in chart_titles:
                meta["sliceNameOverride"] = chart_titles[key]
            pos[cid] = {"type": "CHART", "id": cid, "children": [], "parents": ["ROOT_ID", "GRID_ID", rid], "meta": meta}
            pos[rid]["children"].append(cid)
    return pos


def parse_position_json(position: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a position_json into [{chart_id, row, col, width, height, title}] (inspect mode)."""
    out: list[dict[str, Any]] = []
    grid = position.get("GRID_ID") or {}

    def walk(node_id: str, row: int) -> None:
        node = position.get(node_id) or {}
        for col, child_id in enumerate(node.get("children") or []):
            child = position.get(child_id) or {}
            meta = child.get("meta") or {}
            if child.get("type") == "CHART":
                out.append(
                    {
                        "chart_id": meta.get("chartId"),
                        "row": row,
                        "col": col,
                        "width": meta.get("width"),
                        "height": meta.get("height"),
                        "title": meta.get("sliceNameOverride") or meta.get("sliceName"),
                    }
                )
            elif child.get("type") in {"ROW", "COLUMN", "TABS", "TAB"}:
                walk(child_id, row)

    for r, row_id in enumerate(grid.get("children") or []):
        walk(row_id, r)
    return out
