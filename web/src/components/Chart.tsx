import { useEffect, useMemo, useRef } from "react";
import * as echarts from "echarts/core";
import { BarChart, HeatmapChart, LineChart, PieChart, ScatterChart, TreemapChart } from "echarts/charts";
import { GridComponent, LegendComponent, MarkLineComponent, MarkPointComponent, TitleComponent, TooltipComponent, VisualMapComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { buildOption, DARK, formatKpi, kpiValue, LIGHT, type Preview } from "../lib/charts";
import { usePrefersDark } from "../lib/hooks";
import { DataTable, EmptyState } from "./ui";

echarts.use([BarChart, HeatmapChart, LineChart, PieChart, ScatterChart, TreemapChart, GridComponent, LegendComponent,
  MarkLineComponent, MarkPointComponent, TitleComponent, TooltipComponent, VisualMapComponent, CanvasRenderer]);

let canvasOk: boolean | null = null;
/** False where a 2D canvas is unavailable (e.g. jsdom): charts then fall back to their table. */
export function canvasSupported(): boolean {
  if (canvasOk === null) {
    try {
      canvasOk = typeof document !== "undefined" && !!document.createElement("canvas").getContext?.("2d");
    } catch {
      canvasOk = false;
    }
  }
  return canvasOk;
}

export function EChart({ option, height, label }: { option: object; height: number | string; label: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const inst = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    let chart: echarts.ECharts;
    try {
      chart = echarts.init(ref.current, undefined, { renderer: "canvas" });
    } catch {
      return; // no canvas (e.g. test environment)
    }
    inst.current = chart;
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => chart.resize()) : null;
    ro?.observe(ref.current);
    return () => {
      ro?.disconnect();
      chart.dispose();
      inst.current = null;
    };
  }, []);

  useEffect(() => {
    try {
      inst.current?.setOption(option as echarts.EChartsCoreOption, true);
    } catch (err) {
      console.warn("chart render failed", err);
    }
  }, [option]);

  return <div ref={ref} className="echart" style={{ height }} role="img" aria-label={label} />;
}

export interface ChartViewProps {
  type: string;
  preview: Preview | null | undefined;
  title?: string;
  height?: number | string;
  showTable?: boolean;
}

/** Renders a chart artifact preview: KPI tile, HTML table, or an ECharts chart (with a table view toggle). */
export function ChartView({ type, preview, title, height = 280, showTable = true }: ChartViewProps) {
  const dark = usePrefersDark();
  const option = useMemo(() => buildOption(type, preview, dark ? DARK : LIGHT), [type, preview, dark]);
  if (!preview || !Array.isArray(preview.rows)) return <EmptyState title="No preview data" />;
  if (type === "kpi") {
    return (
      <div className="kpi" aria-label={`${title ?? "KPI"}: ${formatKpi(kpiValue(preview))}`}>
        <div className="kpi-value">{formatKpi(kpiValue(preview))}</div>
        {title && <div className="kpi-label">{title}</div>}
      </div>
    );
  }
  if (type === "table" || !option || !canvasSupported()) {
    return <DataTable columns={preview.columns ?? []} rows={preview.rows} maxRows={50} caption={title} />;
  }
  return (
    <div className="chart-box">
      <EChart option={option} height={height} label={`${type} chart${title ? `: ${title}` : ""}`} />
      {showTable && (
        <details className="chart-table">
          <summary>Table view</summary>
          <DataTable columns={preview.columns ?? []} rows={preview.rows} maxRows={100} caption={title} />
        </details>
      )}
    </div>
  );
}
