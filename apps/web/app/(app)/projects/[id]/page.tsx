"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { LineChart } from "@/components/charts/LineChart";
import { Badge, Card, StateView, TextLink } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useProject } from "@/lib/project";
import type { Target } from "@/lib/types";

interface Summary {
  latest_runs: Record<string, { id: string; status: string; steady_state?: { p95_ms?: number; error_rate?: number; rps?: number } | null }>;
  open_findings: Record<string, number>;
  capacity: { vus: number; reason: string; run_id: string } | null;
  workflows: { discovered: number; approved: number };
}
interface TrendPoint { run_id: string; at: string; value: number | null }

export default function ProjectOverview() {
  const { id, project } = useProject();
  const summary = useQuery({ queryKey: ["summary", id], queryFn: () => apiFetch<Summary>(`/v1/projects/${id}/summary`) });
  const trend = useQuery({ queryKey: ["trend", id], queryFn: () => apiFetch<TrendPoint[]>(`/v1/projects/${id}/trends?metric=p95_ms`) });
  const targets = useQuery({ queryKey: ["targets", id], queryFn: () => apiFetch<Target[]>(`/v1/projects/${id}/targets`) });
  const sev = ["critical", "high", "medium", "low"];
  return (
    <div className="space-y-6">
      <StateView query={summary}>
        {(s) => (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Card><p className="text-xs text-[var(--muted)]">Open findings</p>
              <p className="text-2xl font-semibold">{sev.reduce((n, k) => n + (s.open_findings[k] ?? 0), 0)}</p>
              <p className="mt-1 flex flex-wrap gap-1 text-xs">{sev.filter((k) => s.open_findings[k]).map((k) => <Badge key={k}>{`${s.open_findings[k]} ${k}`}</Badge>)}</p>
              <TextLink href={`/projects/${id}/findings`}>View</TextLink>
            </Card>
            <Card><p className="text-xs text-[var(--muted)]">Last load test p95 (steady state)</p>
              <p className="text-2xl font-semibold">{s.latest_runs.load?.steady_state?.p95_ms ? `${Math.round(s.latest_runs.load.steady_state.p95_ms)} ms` : "—"}</p>
              <p className="text-xs">{s.latest_runs.load ? <Link className="text-[var(--accent)]" href={`/projects/${id}/runs/${s.latest_runs.load.id}`}><Badge>{s.latest_runs.load.status}</Badge></Link> : "no load test yet"} · target {project?.default_slo.p95_ms} ms</p>
            </Card>
            <Card><p className="text-xs text-[var(--muted)]">Measured capacity</p>
              <p className="text-2xl font-semibold">{s.capacity ? `~${s.capacity.vus} VUs` : "—"}</p>
              <p className="text-xs text-[var(--muted)]">{s.capacity ? s.capacity.reason : "run a stress profile to find the knee"}</p>
            </Card>
            <Card><p className="text-xs text-[var(--muted)]">Workflow coverage</p>
              <p className="text-2xl font-semibold">{s.workflows.approved} approved</p>
              <p className="text-xs text-[var(--muted)]">{s.workflows.discovered} discovered · coverage is what you approved, not the whole app</p>
            </Card>
          </div>
        )}
      </StateView>
      {(trend.data?.length ?? 0) > 1 && (
        <LineChart title="p95 latency across load runs (steady state)" unit="ms" format={(v) => `${Math.round(v)} ms`} xLabel="run #"
          threshold={project ? { value: project.default_slo.p95_ms, label: `target ${project.default_slo.p95_ms} ms` } : undefined}
          series={[{ name: "p95", points: trend.data!.map((p, i) => [i + 1, p.value] as [number, number | null]) }]} />
      )}
      <Card title="Targets" actions={<TextLink href={`/projects/${id}/targets`}>Manage</TextLink>}>
        <StateView query={targets} empty={<p>No targets registered yet.</p>}>
          {(list) => <ul className="space-y-1 text-sm">{list.map((t) => <li key={t.id}>{t.host} <Badge>{t.status}</Badge> <Badge tone="info">{t.environment}</Badge></li>)}</ul>}
        </StateView>
      </Card>
    </div>
  );
}
