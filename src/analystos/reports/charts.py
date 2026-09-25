"""Static PNG rendering of ReportChart data for PDF (and any other offline) reports.

Uses matplotlib's object API with the Agg canvas directly (no pyplot global state, no GUI backend),
so it is safe in worker threads. Rendering never raises: malformed or empty data returns None and
callers skip the image. `kpi` and `table` charts return None — they are shown as numbers/tables.
"""
from __future__ import annotations

import io
import logging
import math

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from analystos.contracts.reports import ReportChart
from analystos.reports import _common as C

log = logging.getLogger(__name__)

PALETTE = ["#2f5d8a", "#e07b39", "#4c9a6a", "#8e6bb8", "#c4515e", "#5b9bb0", "#b8a04c", "#7f7f7f",
           "#9c6b4e", "#d88fb2", "#3d7a78", "#a3a3c2"]
TEXT = "#1f2328"
MUTED = "#59636e"
GRID = "#d8dee4"
DPI = 100
MAX_CATEGORIES = 40
MAX_POINTS = 5000
MAX_HEAT = 40
NO_IMAGE_TYPES = {"kpi", "table"}


def _style(ax, *, grid_axis: str | None = "y") -> None:
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    if grid_axis:
        ax.grid(axis=grid_axis, color=GRID, linewidth=0.7)
        ax.set_axisbelow(True)


def _short(s: str, n: int = 24) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _thin_xticks(ax, labels: list[str], max_labels: int = 14) -> None:
    n = len(labels)
    step = max(1, math.ceil(n / max_labels))
    idx = list(range(0, n, step))
    ax.set_xticks(idx)
    ax.set_xticklabels([_short(labels[i], 16) for i in idx], rotation=30 if n > 6 else 0,
                       ha="right" if n > 6 else "center")


def _legend(ax, n_series: int) -> None:
    if n_series > 1:
        ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(1.0, 1.0))


def _line(ax, chart: ReportChart) -> bool:
    w = C.to_wide(chart)
    if not w or not w.x:
        return False
    xs = list(range(len(w.x)))[:MAX_POINTS]
    plotted = 0
    for k, (name, vals) in enumerate(w.series.items()):
        ys = [v if v is not None else float("nan") for v in vals[:MAX_POINTS]]
        if all(math.isnan(y) for y in ys):
            continue
        ax.plot(xs, ys, color=PALETTE[k % len(PALETTE)], linewidth=1.8, label=_short(name),
                marker="o" if len(xs) <= 30 else None, markersize=3)
        plotted += 1
    if not plotted:
        return False
    _thin_xticks(ax, w.x[:MAX_POINTS])
    ax.set_xlabel(w.x_name, color=MUTED, fontsize=9)
    _style(ax)
    _legend(ax, plotted)
    return True


def _bar(ax, chart: ReportChart, stacked: bool) -> bool:
    w = C.to_wide(chart)
    if not w or not w.x:
        return False
    labels = w.x[:MAX_CATEGORIES]
    series = {k: [v or 0.0 for v in vals[:MAX_CATEGORIES]] for k, vals in w.series.items()}
    n = len(series)
    xs = list(range(len(labels)))
    horizontal = n == 1 and not stacked and max((len(s) for s in labels), default=0) > 12
    bottoms = [0.0] * len(labels)
    width = 0.8 if (stacked or n == 1) else 0.8 / n
    for k, (name, vals) in enumerate(series.items()):
        color = PALETTE[k % len(PALETTE)]
        if horizontal:
            ax.barh(xs, vals, color=color, height=0.7)
        elif stacked:
            ax.bar(xs, vals, bottom=bottoms, color=color, width=width, label=_short(name))
            bottoms = [b + v for b, v in zip(bottoms, vals, strict=True)]
        else:
            off = (k - (n - 1) / 2) * width
            ax.bar([x + off for x in xs], vals, color=color, width=width, label=_short(name))
    if horizontal:
        ax.set_yticks(xs)
        ax.set_yticklabels([_short(s, 28) for s in labels])
        ax.invert_yaxis()
        _style(ax, grid_axis="x")
    else:
        _thin_xticks(ax, labels, max_labels=MAX_CATEGORIES)
        _style(ax)
        _legend(ax, n)
    return True


def _histogram(ax, chart: ReportChart) -> bool:
    rows = C.valid_rows(chart)
    if not rows:
        return False
    if len(chart.columns) == 1:  # raw values -> bin here
        vals = [v for v in (C.to_number(r[0]) for r in rows) if v is not None][:MAX_POINTS * 10]
        if not vals:
            return False
        ax.hist(vals, bins=min(30, max(5, int(math.sqrt(len(vals))))), color=PALETTE[0], edgecolor="white")
        ax.set_xlabel(str(chart.columns[0]), color=MUTED, fontsize=9)
        _style(ax)
        return True
    w = C.to_wide(chart)  # pre-binned (bin, count)
    if not w:
        return False
    name, vals = next(iter(w.series.items()))
    labels = w.x[:MAX_CATEGORIES * 2]
    ax.bar(range(len(labels)), [v or 0.0 for v in vals[: len(labels)]], width=0.95, color=PALETTE[0], edgecolor="white")
    _thin_xticks(ax, labels)
    ax.set_xlabel(w.x_name, color=MUTED, fontsize=9)
    ax.set_ylabel(name, color=MUTED, fontsize=9)
    _style(ax)
    return True


def _scatter(ax, chart: ReportChart) -> bool:
    rows = C.valid_rows(chart)
    if len(chart.columns) < 2:
        return False
    pts = [(C.to_number(r[0]), C.to_number(r[1])) for r in rows[:MAX_POINTS]]
    pts = [(x, y) for x, y in pts if x is not None and y is not None]
    if not pts:
        return False
    ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=14, alpha=0.7, color=PALETTE[0], edgecolors="none")
    ax.set_xlabel(str(chart.columns[0]), color=MUTED, fontsize=9)
    ax.set_ylabel(str(chart.columns[1]), color=MUTED, fontsize=9)
    _style(ax, grid_axis="both")
    return True


def _heatmap(fig, ax, chart: ReportChart) -> bool:
    rows = C.valid_rows(chart)
    cols = [str(c) for c in chart.columns]
    if not rows or len(cols) < 2:
        return False
    if len(cols) == 3 and C.to_number(rows[0][2]) is not None:  # long (x, y, value)
        xs: dict[str, None] = {}
        ys: dict[str, None] = {}
        cells: dict[tuple[str, str], float] = {}
        for r in rows:
            x, y, v = C.cell_text(r[0]), C.cell_text(r[1]), C.to_number(r[2])
            if v is None:
                continue
            xs.setdefault(x, None)
            ys.setdefault(y, None)
            cells[(x, y)] = v
        xl, yl = list(xs)[:MAX_HEAT], list(ys)[:MAX_HEAT]
        grid = [[cells.get((x, y), float("nan")) for x in xl] for y in yl]
        xlab, ylab = cols[0], cols[1]
    else:  # wide: row label + numeric columns
        w = C.to_wide(chart)
        if not w:
            return False
        xl = list(w.series)[:MAX_HEAT]
        yl = w.x[:MAX_HEAT]
        grid = [[(w.series[s][i] if w.series[s][i] is not None else float("nan")) for s in xl] for i in range(len(yl))]
        xlab, ylab = "", w.x_name
    if not xl or not yl or all(math.isnan(v) for row in grid for v in row):
        return False
    im = ax.imshow(grid, aspect="auto", cmap="Blues", interpolation="nearest")
    ax.set_xticks(range(len(xl)))
    ax.set_xticklabels([_short(s, 14) for s in xl], rotation=30 if len(xl) > 6 else 0, ha="right" if len(xl) > 6 else "center")
    ax.set_yticks(range(len(yl)))
    ax.set_yticklabels([_short(s, 20) for s in yl])
    ax.set_xlabel(xlab, color=MUTED, fontsize=9)
    ax.set_ylabel(ylab, color=MUTED, fontsize=9)
    ax.tick_params(colors=MUTED, labelsize=8)
    for s in ax.spines.values():
        s.set_visible(False)
    if len(xl) * len(yl) <= 120:
        finite = [v for row in grid for v in row if not math.isnan(v)]
        mid = (min(finite) + max(finite)) / 2
        for i, row in enumerate(grid):
            for j, v in enumerate(row):
                if not math.isnan(v):
                    ax.text(j, i, C.fmt_num(v, 1), ha="center", va="center", fontsize=7,
                            color="white" if v > mid else TEXT)
    fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02).ax.tick_params(labelsize=8, colors=MUTED)
    return True


def _pairs(chart: ReportChart) -> list[tuple[str, float]]:
    w = C.to_wide(chart)
    if not w:
        return []
    vals = next(iter(w.series.values()))
    agg: dict[str, float] = {}
    for label, v in zip(w.x, vals, strict=True):
        if v is not None and v > 0:
            agg[label] = agg.get(label, 0.0) + v
    return sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))


def _pie(ax, chart: ReportChart) -> bool:
    pairs = _pairs(chart)
    if not pairs:
        return False
    if len(pairs) > 8:
        pairs = pairs[:7] + [("Other", sum(v for _, v in pairs[7:]))]
    ax.pie([v for _, v in pairs], labels=[_short(k, 20) for k, _ in pairs], colors=PALETTE[: len(pairs)],
           autopct=lambda p: f"{p:.0f}%" if p >= 4 else "", startangle=90, counterclock=False,
           wedgeprops={"edgecolor": "white", "linewidth": 1}, textprops={"fontsize": 8, "color": TEXT})
    ax.set_aspect("equal")
    return True


def _squarify(values: list[float], x: float, y: float, w: float, h: float) -> list[tuple[float, float, float, float]]:
    """Squarified treemap layout (Bruls et al.) for values sorted descending; returns rects in order."""
    total = sum(values)
    if total <= 0:
        return []
    scaled = [v * w * h / total for v in values]
    rects: list[tuple[float, float, float, float]] = []

    def worst(row: list[float], side: float) -> float:
        s = sum(row)
        return max(max(side * side * r / (s * s), (s * s) / (side * side * r)) for r in row)

    i = 0
    while i < len(scaled):
        side = min(w, h)
        row = [scaled[i]]
        i += 1
        while i < len(scaled) and worst(row + [scaled[i]], side) <= worst(row, side):
            row.append(scaled[i])
            i += 1
        s = sum(row)
        if w >= h:  # lay the row out as a column on the left
            cw = s / h
            cy = y
            for r in row:
                rects.append((x, cy, cw, r / cw))
                cy += r / cw
            x, w = x + cw, w - cw
        else:
            rh = s / w
            cx = x
            for r in row:
                rects.append((cx, y, r / rh, rh))
                cx += r / rh
            y, h = y + rh, h - rh
    return rects


def _treemap(ax, chart: ReportChart) -> bool:
    from matplotlib.patches import Rectangle

    pairs = _pairs(chart)[:MAX_CATEGORIES]
    if not pairs:
        return False
    rects = _squarify([v for _, v in pairs], 0, 0, 100, 60)
    for k, ((label, v), (rx, ry, rw, rh)) in enumerate(zip(pairs, rects, strict=True)):
        ax.add_patch(Rectangle((rx, ry), rw, rh, facecolor=PALETTE[k % len(PALETTE)], edgecolor="white", linewidth=1.5))
        if rw > 7 and rh > 5:
            ax.text(rx + 1, ry + 1, f"{_short(label, max(4, int(rw / 1.6)))}\n{C.fmt_num(v, 1)}",
                    ha="left", va="top", fontsize=8, color="white")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 60)
    ax.invert_yaxis()
    ax.axis("off")
    return True


def render_chart_png(chart: ReportChart, *, width_px: int = 900, height_px: int = 420) -> bytes | None:
    """PNG bytes for a chart, or None when the type has no image (kpi/table) or the data is unusable."""
    ctype = (chart.chart_type or "").strip().lower()
    if ctype in NO_IMAGE_TYPES:
        return None
    try:
        width_px = max(200, min(int(width_px), 4000))
        height_px = max(150, min(int(height_px), 3000))
        fig = Figure(figsize=(width_px / DPI, height_px / DPI), dpi=DPI, facecolor="white")
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(1, 1, 1)
        if ctype == "line":
            ok = _line(ax, chart)
        elif ctype in ("bar", "stacked_bar"):
            ok = _bar(ax, chart, stacked=ctype == "stacked_bar")
        elif ctype == "histogram":
            ok = _histogram(ax, chart)
        elif ctype == "scatter":
            ok = _scatter(ax, chart)
        elif ctype == "heatmap":
            ok = _heatmap(fig, ax, chart)
        elif ctype == "pie":
            ok = _pie(ax, chart)
        elif ctype == "treemap":
            ok = _treemap(ax, chart)
        else:
            ok = False
        if not ok:
            return None
        ax.set_title(_short(chart.title or "", 90), loc="left", fontsize=12, color=TEXT, pad=10)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=DPI, facecolor="white", metadata={"Software": None})
        return buf.getvalue()
    except Exception as e:  # never let one bad chart break a report
        log.warning("report chart %r (%s) not rendered: %s", chart.key, ctype, e)
        return None
