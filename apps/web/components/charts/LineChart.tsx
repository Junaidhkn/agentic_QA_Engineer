"use client";

import { useEffect, useMemo, useRef, useState } from "react";

/**
 * Single-axis line chart (ECharts). Follows the data-viz method: categorical slots in fixed
 * order, 2px lines, legend for >= 2 series plus direct end labels (<= 4 series), crosshair
 * tooltip, recessive grid, threshold as a dashed reference line, optional shaded window, and a
 * table view for accessibility. Never dual-axis — callers render separate charts per unit.
 */
export interface Series { name: string; points: Array<[number, number | null]> }

interface Props {
  title: string;
  series: Series[];
  unit: string;
  format?: (v: number) => string;
  threshold?: { value: number; label: string };
  band?: { from: number; to: number; label: string };
  height?: number;
  xLabel?: string;
}

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

export function LineChart({ title, series, unit, format, threshold, band, height = 220, xLabel = "seconds since start" }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [table, setTable] = useState(false);
  const fmt = useMemo(() => format ?? ((v: number) => `${Math.round(v * 100) / 100}${unit ? ` ${unit}` : ""}`), [format, unit]);

  useEffect(() => {
    if (table || !ref.current) return;
    let disposed = false;
    let chart: { setOption: (o: unknown) => void; resize: () => void; dispose: () => void } | null = null;
    const onResize = () => chart?.resize();
    void import("echarts").then((echarts) => {
      if (disposed || !ref.current) return;
      chart = echarts.init(ref.current, undefined, { renderer: "svg" });
      const colors = ["--series-1", "--series-2", "--series-3"].map((v, i) => cssVar(v, ["#2a78d6", "#eb6834", "#1baf7a"][i]));
      const axis = cssVar("--chart-axis", "#52514e");
      const grid = cssVar("--chart-grid", "#e7e6e2");
      const muted = cssVar("--chart-muted", "#8a8984");
      const multi = series.length > 1;
      chart.setOption({
        animation: false,
        color: colors,
        grid: { left: 56, right: multi ? 64 : 16, top: multi ? 32 : 12, bottom: 36 },
        legend: multi ? { top: 0, left: 0, textStyle: { color: axis }, icon: "roundRect", itemWidth: 12, itemHeight: 4 } : undefined,
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "line", lineStyle: { color: muted } },
          valueFormatter: (v: number | null) => (v === null || v === undefined ? "—" : fmt(v)),
        },
        xAxis: {
          type: "value", name: xLabel, nameLocation: "middle", nameGap: 24,
          nameTextStyle: { color: axis }, axisLabel: { color: axis }, axisLine: { lineStyle: { color: grid } },
          splitLine: { show: false }, min: "dataMin", max: "dataMax",
        },
        yAxis: {
          type: "value", axisLabel: { color: axis, formatter: (v: number) => fmt(v) },
          splitLine: { lineStyle: { color: grid } },
        },
        series: series.map((s, i) => ({
          name: s.name, type: "line", data: s.points, showSymbol: false, symbolSize: 8, connectNulls: false,
          lineStyle: { width: 2 },
          endLabel: multi && series.length <= 4 ? { show: true, formatter: s.name, color: axis } : undefined,
          emphasis: { focus: "series" },
          markLine: i === 0 && threshold ? {
            symbol: "none", silent: true, label: { formatter: threshold.label, color: axis, position: "insideEndTop" },
            lineStyle: { type: "dashed", color: muted, width: 1 }, data: [{ yAxis: threshold.value }],
          } : undefined,
          markArea: i === 0 && band ? {
            silent: true, itemStyle: { color: cssVar("--chart-band", "rgba(42,120,214,0.08)") },
            label: { show: true, color: axis, position: "insideTop", formatter: band.label },
            data: [[{ xAxis: band.from }, { xAxis: band.to }]],
          } : undefined,
        })),
      });
      window.addEventListener("resize", onResize);
    });
    return () => { disposed = true; window.removeEventListener("resize", onResize); chart?.dispose(); };
  }, [series, threshold, band, table, unit, xLabel, fmt]);

  const xs = Array.from(new Set(series.flatMap((s) => s.points.map((p) => p[0])))).sort((a, b) => a - b);
  return (
    <figure className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-3">
      <figcaption className="mb-2 flex items-center justify-between text-sm font-medium">
        <span>{title}</span>
        <button className="text-xs text-[var(--muted)] underline" onClick={() => setTable(!table)}>{table ? "View chart" : "View as table"}</button>
      </figcaption>
      {table ? (
        <div className="max-h-64 overflow-auto">
          <table className="w-full text-xs">
            <thead><tr><th className="text-left">t (s)</th>{series.map((s) => <th key={s.name} className="text-right">{s.name}</th>)}</tr></thead>
            <tbody>
              {xs.map((x) => (
                <tr key={x}><td>{x}</td>{series.map((s) => { const v = s.points.find((p) => p[0] === x)?.[1]; return <td key={s.name} className="text-right">{v === null || v === undefined ? "—" : fmt(v)}</td>; })}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div ref={ref} style={{ height }} role="img" aria-label={`${title} chart; use "View as table" for values`} />
      )}
    </figure>
  );
}
