/**
 * ECharts option builders for chart artifacts (ChartSpec.preview = {columns, rows}).
 *
 * Pure functions so they can be unit-tested without a canvas. Colours come from a palette object
 * (light / dark are separately stepped, not an automatic flip). Categorical hues are assigned in a
 * fixed order and never cycled: series beyond the palette fold into "Other".
 */

export type ChartType =
  | "kpi" | "line" | "bar" | "stacked_bar" | "histogram" | "scatter" | "heatmap" | "table" | "pie" | "treemap";

export interface Preview {
  columns: string[];
  rows: unknown[][];
}

export interface ChartPalette {
  series: string[];
  sequential: string[]; // light -> dark
  text: string;
  textMuted: string;
  grid: string;
  surface: string;
  tooltipBg: string;
}

export const LIGHT: ChartPalette = {
  series: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
  sequential: ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"],
  text: "#0b0b0b",
  textMuted: "#52514e",
  grid: "#e4e3df",
  surface: "#fcfcfb",
  tooltipBg: "#ffffff",
};

export const DARK: ChartPalette = {
  series: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
  sequential: ["#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"],
  text: "#ffffff",
  textMuted: "#c3c2b7",
  grid: "#383835",
  surface: "#1a1a19",
  tooltipBg: "#262624",
};

export const MAX_SERIES = 8;

export function toNumber(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "boolean") return v ? 1 : 0;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function label(v: unknown): string {
  if (v === null || v === undefined) return "(none)";
  // Midnight ISO timestamps (monthly/daily buckets) read better as dates.
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}[T ]00:00:00(\.0+)?(Z|[+-]00:?00)?$/.test(v)) return v.slice(0, 10);
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  return String(v);
}

/** The big number for a KPI preview (first numeric cell of the first row). */
export function kpiValue(p: Preview | undefined | null): number | null {
  if (!p || !p.rows?.length) return null;
  for (const cell of p.rows[0]) {
    const n = toNumber(cell);
    if (n !== null) return n;
  }
  return null;
}

export function formatKpi(n: number | null): string {
  if (n === null) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (abs >= 1e4) return `${(n / 1e3).toFixed(1)}K`;
  if (Number.isInteger(n)) return n.toLocaleString("en-US");
  if (abs < 1) return n.toFixed(3);
  return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function axisCommon(p: ChartPalette) {
  return {
    axisLine: { lineStyle: { color: p.grid } },
    axisTick: { show: false },
    axisLabel: { color: p.textMuted, fontSize: 11, hideOverlap: true },
    splitLine: { lineStyle: { color: p.grid, type: "dashed" as const } },
    nameTextStyle: { color: p.textMuted, fontSize: 11 },
  };
}

function base(p: ChartPalette) {
  return {
    backgroundColor: "transparent",
    textStyle: { color: p.text, fontFamily: "inherit" },
    color: p.series,
    animationDuration: 300,
    tooltip: {
      backgroundColor: p.tooltipBg,
      borderColor: p.grid,
      textStyle: { color: p.text, fontSize: 12 },
      confine: true,
    },
    grid: { left: 8, right: 16, top: 28, bottom: 8, containLabel: true },
  };
}

/** Pivot rows [x, series, y] into one series per `series` value, folding the tail into "Other". */
export function pivot(rows: unknown[][]): { xs: string[]; series: { name: string; data: (number | null)[] }[] } {
  const xs: string[] = [];
  const totals = new Map<string, number>();
  for (const r of rows) {
    const x = label(r[0]);
    if (!xs.includes(x)) xs.push(x);
    const s = label(r[1]);
    totals.set(s, (totals.get(s) ?? 0) + Math.abs(toNumber(r[2]) ?? 0));
  }
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k);
  const keep = ranked.length > MAX_SERIES ? ranked.slice(0, MAX_SERIES - 1) : ranked;
  const names = ranked.length > MAX_SERIES ? [...keep, "Other"] : keep;
  const data = new Map(names.map((n) => [n, xs.map(() => null as number | null)]));
  for (const r of rows) {
    const s = keep.includes(label(r[1])) ? label(r[1]) : "Other";
    const arr = data.get(s)!;
    const i = xs.indexOf(label(r[0]));
    arr[i] = (arr[i] ?? 0) + (toNumber(r[2]) ?? 0);
  }
  return { xs, series: names.map((name) => ({ name, data: data.get(name)! })) };
}

/**
 * Build an ECharts option for a chart type + preview. Returns null for types rendered as HTML
 * (kpi, table) or when the preview is empty.
 */
export function buildOption(type: string, preview: Preview | undefined | null, palette: ChartPalette = LIGHT, title?: string) {
  if (!preview || !Array.isArray(preview.rows) || preview.rows.length === 0) return null;
  const { columns, rows } = preview;
  const p = palette;
  const b = base(p);
  const ax = axisCommon(p);
  const xName = columns[0] ?? "x";
  const yName = columns[columns.length > 2 && type !== "heatmap" && type !== "stacked_bar" ? 1 : columns.length - 1] ?? "y";
  const titleOpt = title ? { title: { text: title, left: 0, top: 0, textStyle: { fontSize: 13, fontWeight: 600, color: p.text } } } : {};

  switch (type as ChartType) {
    case "kpi":
    case "table":
      return null;
    case "line":
      return {
        ...b,
        ...titleOpt,
        tooltip: { ...b.tooltip, trigger: "axis", axisPointer: { type: "line", lineStyle: { color: p.textMuted } } },
        xAxis: { type: "category", data: rows.map((r) => label(r[0])), name: xName, boundaryGap: false, ...ax, splitLine: { show: false } },
        yAxis: { type: "value", ...ax },
        series: [{ type: "line", name: yName, data: rows.map((r) => toNumber(r[1])), showSymbol: rows.length <= 24,
          symbolSize: 8, lineStyle: { width: 2 }, smooth: false }],
      };
    case "bar":
    case "histogram": {
      const histogram = type === "histogram";
      return {
        ...b,
        ...titleOpt,
        tooltip: { ...b.tooltip, trigger: "axis", axisPointer: { type: "shadow" } },
        xAxis: { type: "category", data: rows.map((r) => label(r[0])), name: xName, ...ax, splitLine: { show: false } },
        yAxis: { type: "value", name: histogram ? "count" : undefined, ...ax },
        series: [{
          type: "bar", name: histogram ? "count" : yName, data: rows.map((r) => toNumber(r[1])),
          barCategoryGap: histogram ? "4%" : "30%", barMaxWidth: 48,
          itemStyle: { borderRadius: [4, 4, 0, 0], color: p.series[0] },
        }],
      };
    }
    case "stacked_bar": {
      if (columns.length >= 3) {
        const { xs, series } = pivot(rows);
        return {
          ...b,
          ...titleOpt,
          legend: { top: title ? 22 : 0, textStyle: { color: p.textMuted }, type: "scroll" },
          grid: { ...b.grid, top: title ? 52 : 30 },
          tooltip: { ...b.tooltip, trigger: "axis", axisPointer: { type: "shadow" } },
          xAxis: { type: "category", data: xs, name: xName, ...ax, splitLine: { show: false } },
          yAxis: { type: "value", ...ax },
          series: series.map((s, i) => ({
            type: "bar", stack: "total", name: s.name, data: s.data, barMaxWidth: 48,
            itemStyle: { color: p.series[i], borderColor: p.surface, borderWidth: 1 },
          })),
        };
      }
      return buildOption("bar", preview, palette, title);
    }
    case "scatter":
      return {
        ...b,
        ...titleOpt,
        tooltip: { ...b.tooltip, trigger: "item" },
        xAxis: { type: "value", name: xName, scale: true, ...ax },
        yAxis: { type: "value", name: yName, scale: true, ...ax },
        series: [{ type: "scatter", symbolSize: 8, data: rows.map((r) => [toNumber(r[0]), toNumber(r[1])]),
          itemStyle: { color: p.series[0], borderColor: p.surface, borderWidth: 1, opacity: 0.85 } }],
      };
    case "heatmap": {
      const xs = [...new Set(rows.map((r) => label(r[0])))];
      const ys = [...new Set(rows.map((r) => label(r[1])))];
      const vals = rows.map((r) => toNumber(r[2]) ?? 0);
      return {
        ...b,
        ...titleOpt,
        tooltip: { ...b.tooltip, trigger: "item" },
        grid: { ...b.grid, bottom: 44 },
        xAxis: { type: "category", data: xs, ...ax, splitLine: { show: false } },
        yAxis: { type: "category", data: ys, ...ax, splitLine: { show: false } },
        visualMap: { min: Math.min(...vals), max: Math.max(...vals), calculable: false, orient: "horizontal", left: "center",
          bottom: 0, itemHeight: 120, textStyle: { color: p.textMuted }, inRange: { color: p.sequential } },
        series: [{ type: "heatmap", data: rows.map((r) => [xs.indexOf(label(r[0])), ys.indexOf(label(r[1])), toNumber(r[2]) ?? 0]),
          itemStyle: { borderColor: p.surface, borderWidth: 2 } }],
      };
    }
    case "pie": {
      const sorted = [...rows].sort((a, b2) => (toNumber(b2[1]) ?? 0) - (toNumber(a[1]) ?? 0));
      const head = sorted.slice(0, MAX_SERIES - 1).map((r) => ({ name: label(r[0]), value: toNumber(r[1]) ?? 0 }));
      const tail = sorted.slice(MAX_SERIES - 1).reduce((s, r) => s + (toNumber(r[1]) ?? 0), 0);
      const data = tail > 0 ? [...head, { name: "Other", value: tail }] : head;
      return {
        ...b,
        ...titleOpt,
        tooltip: { ...b.tooltip, trigger: "item", formatter: "{b}: {c} ({d}%)" },
        legend: { bottom: 0, type: "scroll", textStyle: { color: p.textMuted } },
        series: [{ type: "pie", radius: ["40%", "68%"], center: ["50%", "46%"], data,
          itemStyle: { borderColor: p.surface, borderWidth: 2 }, label: { color: p.textMuted } }],
      };
    }
    case "treemap":
      return {
        ...b,
        ...titleOpt,
        color: undefined,
        tooltip: { ...b.tooltip, trigger: "item" },
        series: [{
          type: "treemap", roam: false, nodeClick: false, breadcrumb: { show: false }, top: title ? 26 : 0, left: 0, right: 0, bottom: 0,
          data: rows.map((r) => ({ name: label(r[0]), value: toNumber(r[1]) ?? 0 })),
          levels: [{ color: p.sequential.slice(2), colorMappingBy: "value", itemStyle: { borderColor: p.surface, borderWidth: 2, gapWidth: 2 } }],
          label: { color: "#ffffff", fontSize: 11 },
        }],
      };
    default:
      // Unknown type: fall back to a bar when the preview looks like (label, number).
      if (columns.length >= 2 && rows.every((r) => toNumber(r[1]) !== null)) return buildOption("bar", preview, palette, title);
      return null;
  }
}

/** Pick a sensible chart for an ad-hoc result (Ask box). Returns the chart type or null. */
export function guessChart(columns: string[], rows: unknown[][], hint?: string | null): ChartType | null {
  if (!rows.length || columns.length === 0) return null;
  const allowed: ChartType[] = ["kpi", "line", "bar", "pie", "scatter", "table"];
  if (hint && (allowed as string[]).includes(hint)) return hint as ChartType;
  if (rows.length === 1 && columns.length === 1 && toNumber(rows[0][0]) !== null) return "kpi";
  if (columns.length >= 2 && rows.every((r) => toNumber(r[1]) !== null)) {
    const first = rows.map((r) => r[0]);
    const looksTime = first.every((v) => typeof v === "string" && /^\d{4}-\d{2}/.test(v));
    return looksTime ? "line" : "bar";
  }
  return null;
}
