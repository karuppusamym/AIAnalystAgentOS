import { describe, expect, it } from "vitest";
import { buildOption, DARK, formatKpi, guessChart, kpiValue, LIGHT, MAX_SERIES, pivot } from "../lib/charts";

describe("chart option builders", () => {
  it("returns null for html-rendered types and empty previews", () => {
    expect(buildOption("kpi", { columns: ["value"], rows: [[3]] })).toBeNull();
    expect(buildOption("table", { columns: ["a"], rows: [[1]] })).toBeNull();
    expect(buildOption("bar", { columns: ["x", "y"], rows: [] })).toBeNull();
  });

  it("builds a line chart from x/y columns", () => {
    const o = buildOption("line", { columns: ["x", "y"], rows: [["2026-01", 3], ["2026-02", "4.5"]] }) as any;
    expect(o.xAxis.data).toEqual(["2026-01", "2026-02"]);
    expect(o.series[0].type).toBe("line");
    expect(o.series[0].data).toEqual([3, 4.5]);
    expect(o.series[0].lineStyle.width).toBe(2);
  });

  it("uses the dark palette when asked", () => {
    const o = buildOption("bar", { columns: ["x", "y"], rows: [["a", 1]] }, DARK) as any;
    expect(o.series[0].itemStyle.color).toBe(DARK.series[0]);
    expect(LIGHT.series[0]).not.toBe(DARK.series[0]);
  });

  it("indexes heatmap cells by category position", () => {
    const o = buildOption("heatmap", { columns: ["x", "y", "v"], rows: [["Mon", "P1", 2], ["Tue", "P2", 5]] }) as any;
    expect(o.series[0].data).toEqual([[0, 0, 2], [1, 1, 5]]);
    expect(o.visualMap.max).toBe(5);
  });

  it("folds pie slices beyond the palette into Other", () => {
    const rows = Array.from({ length: 12 }, (_, i) => [`s${i}`, 12 - i]);
    const o = buildOption("pie", { columns: ["x", "y"], rows }) as any;
    expect(o.series[0].data).toHaveLength(MAX_SERIES);
    expect(o.series[0].data.at(-1).name).toBe("Other");
  });

  it("pivots stacked series and never exceeds the palette", () => {
    const rows = [];
    for (let s = 0; s < 10; s++) rows.push(["jan", `g${s}`, s + 1], ["feb", `g${s}`, s + 2]);
    const { xs, series } = pivot(rows);
    expect(xs).toEqual(["jan", "feb"]);
    expect(series).toHaveLength(MAX_SERIES);
    expect(series.at(-1)!.name).toBe("Other");
    const o = buildOption("stacked_bar", { columns: ["x", "s", "y"], rows }) as any;
    expect(o.series.every((s: any) => s.stack === "total")).toBe(true);
  });

  it("reads and formats KPI values", () => {
    expect(kpiValue({ columns: ["value"], rows: [["12.5"]] })).toBe(12.5);
    expect(kpiValue({ columns: ["value"], rows: [] })).toBeNull();
    expect(formatKpi(1234)).toBe("1,234");
    expect(formatKpi(2_500_000)).toBe("2.5M");
    expect(formatKpi(null)).toBe("—");
  });

  it("guesses a chart for ad-hoc results", () => {
    expect(guessChart(["n"], [[5]])).toBe("kpi");
    expect(guessChart(["month", "n"], [["2026-01-01", 1], ["2026-02-01", 2]])).toBe("line");
    expect(guessChart(["group", "n"], [["a", 1]])).toBe("bar");
    expect(guessChart(["a", "b"], [["x", "y"]])).toBeNull();
    expect(guessChart(["a", "b"], [["x", 1]], "pie")).toBe("pie");
  });
});

describe("labels", () => {
  it("shortens midnight timestamps to dates", async () => {
    const { label } = await import("../lib/charts");
    expect(label("2025-09-01T00:00:00")).toBe("2025-09-01");
    expect(label("2025-09-01T10:30:00")).toBe("2025-09-01T10:30:00");
    expect(label(1.5)).toBe("1.50");
  });
});
