"use client";

import { LineChart } from "@/components/charts/LineChart";
import { Badge } from "@/components/ui";
import { openArtifact } from "@/lib/download";
import type { EvidenceItem, SeriesPoint } from "@/lib/types";

const ms = (v: number) => `${Math.round(v)} ms`;

/** Renders one evidence item. Summaries are deterministic facts (never LLM prose). */
export function EvidenceCard({ e, index }: { e: EvidenceItem; index?: number }) {
  const series = (e.snapshot?.series as SeriesPoint[] | undefined) ?? [];
  const artifactId = e.locator.artifact_id as string | undefined;
  return (
    <article id={`evidence-${e.id}`} className="rounded-md border border-[var(--border)] p-3">
      <header className="mb-1 flex items-center gap-2 text-xs text-[var(--muted)]">
        {index !== undefined && <span className="font-mono">E{index + 1}</span>}
        <Badge tone="info">{e.kind.replace("_", " ")}</Badge>
        <span>collected by {e.collected_by}</span>
      </header>
      <p className="text-sm">{e.summary}</p>
      {e.kind === "metric_window" && series.length > 1 && (
        <div className="mt-2">
          <LineChart title="p95 latency during the run" height={140} unit="ms" format={ms}
            series={[{ name: "p95", points: series.map((p) => [p.t, p.p95_ms] as [number, number | null]) }]} />
        </div>
      )}
      {e.kind === "assertion" && typeof e.snapshot?.error === "string" && (
        <pre className="mt-2 max-h-48 overflow-auto rounded bg-black/5 p-2 text-xs whitespace-pre-wrap">{e.snapshot.error}</pre>
      )}
      {(e.kind === "http_sample" || e.kind === "trace") && Boolean(e.locator.trace_id) && (
        <p className="mt-1 font-mono text-xs">trace {String(e.locator.trace_id)}</p>
      )}
      {artifactId && <button className="mt-1 text-xs text-[var(--accent)] underline" onClick={() => openArtifact(artifactId)}>Open artifact</button>}
      {e.kind === "experiment" && e.snapshot && <pre className="mt-2 overflow-auto text-xs">{JSON.stringify(e.snapshot, null, 2)}</pre>}
    </article>
  );
}
