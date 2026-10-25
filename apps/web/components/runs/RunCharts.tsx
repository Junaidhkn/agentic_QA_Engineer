"use client";

import { useMemo } from "react";

import { LineChart } from "@/components/charts/LineChart";
import type { SeriesPoint } from "@/lib/types";

const pct = (v: number) => `${(v * 100).toFixed(1)}%`;
const ms = (v: number) => `${Math.round(v)} ms`;
const num = (v: number) => `${Math.round(v * 10) / 10}`;

/** Four single-axis charts instead of one dual-axis chart (latency, throughput, users, errors). */
export function RunCharts({ points, p95Threshold, steadyWindow }: { points: SeriesPoint[]; p95Threshold?: number; steadyWindow?: [number, number] | null }) {
  const latency = useMemo(() => [
    { name: "p50", points: points.map((p) => [p.t, p.p50_ms] as [number, number | null]) },
    { name: "p95", points: points.map((p) => [p.t, p.p95_ms] as [number, number | null]) },
    { name: "p99", points: points.map((p) => [p.t, p.p99_ms] as [number, number | null]) },
  ], [points]);
  const rps = useMemo(() => [{ name: "requests/s", points: points.map((p) => [p.t, p.rps] as [number, number]) }], [points]);
  const vus = useMemo(() => [{ name: "virtual users", points: points.map((p) => [p.t, p.vus] as [number, number]) }], [points]);
  const errors = useMemo(() => [{ name: "failed requests", points: points.map((p) => [p.t, p.error_rate] as [number, number]) }], [points]);
  const band = steadyWindow ? { from: steadyWindow[0], to: steadyWindow[1], label: "steady state" } : undefined;
  const threshold = p95Threshold ? { value: p95Threshold, label: `p95 target ${p95Threshold} ms` } : undefined;
  if (!points.length) return <p className="text-sm text-[var(--muted)]">No time-series data yet.</p>;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <LineChart title="Latency" series={latency} unit="ms" format={ms} threshold={threshold} band={band} />
      <LineChart title="Throughput (requests/s)" series={rps} unit="" format={num} band={band} />
      <LineChart title="Virtual users" series={vus} unit="" format={num} />
      <LineChart title="Failed requests" series={errors} unit="" format={pct} band={band} />
    </div>
  );
}
