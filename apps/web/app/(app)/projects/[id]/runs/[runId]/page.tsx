"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { useState } from "react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { LiveRun } from "@/components/runs/LiveRun";
import { RunCharts } from "@/components/runs/RunCharts";
import { PlanView, ReportView } from "@/components/sandbox/SandboxDetails";
import { Badge, Button, Card, ErrorNote, fmtDate, StateView, Table } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { openArtifact } from "@/lib/download";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { Finding, Run, RunArtifact, RunMetrics, RunSummaryRow } from "@/lib/types";
import Link from "next/link";

const ACTIVE = new Set(["queued", "provisioning", "running", "cancelling", "analyzing", "pending_approval"]);
const ms = (v?: number | null) => (v === null || v === undefined ? "—" : `${Math.round(v)} ms`);

export default function RunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const { role } = useProject();
  const qc = useQueryClient();
  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => apiFetch<Run>(`/v1/runs/${runId}`),
    refetchInterval: (q) => (q.state.data && ACTIVE.has(q.state.data.status) ? 2000 : false),
  });
  const finished = run.data && !ACTIVE.has(run.data.status);
  const summaries = useQuery({ queryKey: ["run-summaries", runId], queryFn: () => apiFetch<RunSummaryRow[]>(`/v1/runs/${runId}/summaries`), enabled: Boolean(finished) });
  const [scope, setScope] = useState("overall");
  const metrics = useQuery({ queryKey: ["run-metrics", runId, scope], queryFn: () => apiFetch<RunMetrics>(`/v1/runs/${runId}/metrics?scope=${encodeURIComponent(scope)}`), enabled: Boolean(finished) && run.data?.kind === "load" });
  const findings = useQuery({ queryKey: ["run-findings", runId, run.data?.project_id], queryFn: () => apiFetch<Finding[]>(`/v1/projects/${run.data!.project_id}/findings`), enabled: Boolean(finished) });
  const artifacts = useQuery({ queryKey: ["run-artifacts", runId], queryFn: () => apiFetch<RunArtifact[]>(`/v1/runs/${runId}/artifacts`), enabled: Boolean(finished) });
  const [stopping, setStopping] = useState(false);
  const stop = useMutation({
    mutationFn: () => apiFetch<Run>(`/v1/runs/${runId}:cancel`, { method: "POST", body: JSON.stringify({ reason: "stopped by user" }) }),
    onSuccess: () => { setStopping(false); qc.invalidateQueries({ queryKey: ["run", runId] }); },
  });

  return (
    <StateView query={run}>
      {(r) => {
        const s = (r.summary ?? {}) as Record<string, unknown>;
        const overall = (s.overall ?? {}) as { p50_ms?: number; p95_ms?: number; p99_ms?: number; error_rate?: number; requests?: number };
        const failed = (s.failed_thresholds ?? []) as { metric: string; threshold: string; observed: number | null }[];
        return (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-xl font-semibold">Run {r.id.slice(0, 8)} <Badge>{r.status}</Badge></h2>
                <p className="text-sm text-[var(--muted)]">{r.kind}{r.profile ? ` · ${String(r.profile.type)} · up to ${String(r.profile.max_vus)} VUs` : ""} · {String(r.target?.host ?? r.environment_kind)} · started {fmtDate(r.started_at)}</p>
                {r.status_reason && <p className="mt-1 text-sm">{r.status_reason}</p>}
              </div>
              {hasRole(role, "engineer") && ["provisioning", "running", "queued", "pending_approval"].includes(r.status) && (
                <Button variant="danger" onClick={() => setStopping(true)}>Stop</Button>
              )}
            </div>
            {ACTIVE.has(r.status) && <LiveRun runId={r.id} p95Threshold={Number((r.profile?.thresholds as Record<string, number> | undefined)?.p95_ms) || undefined} onStatus={() => qc.invalidateQueries({ queryKey: ["run", runId] })} />}
            {finished && r.kind === "load" && (
              <div className="grid gap-4 sm:grid-cols-4">
                {[["p50", ms(overall.p50_ms)], ["p95", ms(overall.p95_ms)], ["p99", ms(overall.p99_ms)], ["Failed requests", overall.error_rate !== undefined ? `${(overall.error_rate * 100).toFixed(2)}%` : "—"]].map(([k, v]) => (
                  <Card key={k}><p className="text-xs text-[var(--muted)]">{k}</p><p className="text-2xl font-semibold">{v}</p></Card>
                ))}
              </div>
            )}
            {finished && r.kind === "load" && metrics.data && (
              <div className="space-y-2">
                <label className="text-sm">Scope{" "}
                  <select className="rounded border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-sm" value={scope} onChange={(e) => setScope(e.target.value)}>
                    {metrics.data.scopes.map((sc) => <option key={sc} value={sc}>{sc === "overall" ? "All requests" : sc.replace("endpoint:", "")}</option>)}
                  </select>
                </label>
                <RunCharts points={metrics.data.points} steadyWindow={metrics.data.steady_state?.window_s ?? null} p95Threshold={Number((r.profile?.thresholds as Record<string, number> | undefined)?.p95_ms) || undefined} />
                {Boolean(s.knee) && <p className="text-sm"><strong>Measured capacity:</strong> latency broke down at about {String((s.knee as { vus: number }).vus)} virtual users ({String((s.knee as { reason: string }).reason)}).</p>}
              </div>
            )}
            {finished && (findings.data ?? []).filter((f) => f.last_run_id === r.id).length > 0 && (
              <Card title="Findings from this run">
                <ul className="space-y-1 text-sm">
                  {(findings.data ?? []).filter((f) => f.last_run_id === r.id).map((f) => (
                    <li key={f.id}><Badge>{f.severity}</Badge> <Link className="text-[var(--accent)]" href={`/projects/${r.project_id}/findings/${f.id}`}>{f.title}</Link></li>
                  ))}
                </ul>
              </Card>
            )}
            {r.generator_health && (r.generator_health as { valid?: boolean }).valid === false && (
              <p role="alert" className="rounded-md bg-amber-50 p-3 text-sm text-amber-900">The load generator was saturated, so these numbers describe our generator, not your app. Re-run with a larger generator.</p>
            )}
            {failed.length > 0 && (
              <Card title="Failed thresholds">
                <ul className="list-disc pl-5 text-sm">{failed.map((f) => <li key={f.metric + f.threshold}><code>{f.metric}</code> {f.threshold} — observed {f.observed === null ? "n/a" : Math.round(f.observed * 1000) / 1000}</li>)}</ul>
              </Card>
            )}
            {finished && (summaries.data?.length ?? 0) > 0 && (
              <Card title="By workflow and endpoint">
                <Table head={["Scope", "Requests", "RPS", "Errors", "p50", "p95", "p99"]}>
                  {summaries.data!.map((row) => (
                    <tr key={row.scope + row.time_window}>
                      <td className="px-2 py-2 font-mono text-xs">{row.scope}</td>
                      <td className="px-2 py-2">{row.requests}</td>
                      <td className="px-2 py-2">{row.rps.toFixed(1)}</td>
                      <td className="px-2 py-2">{(row.error_rate * 100).toFixed(2)}%</td>
                      <td className="px-2 py-2">{ms(row.p50_ms)}</td>
                      <td className="px-2 py-2">{ms(row.p95_ms)}</td>
                      <td className="px-2 py-2">{ms(row.p99_ms)}</td>
                    </tr>
                  ))}
                </Table>
              </Card>
            )}
            {r.environment_kind === "sandbox_build" && r.build_ref && (
              <Card title="Sandbox">
                <div className="space-y-4">
                  {(r.build_ref.report as Record<string, unknown> | undefined) && <ReportView report={r.build_ref.report as Record<string, unknown>} />}
                  {(r.build_ref.plan as Record<string, unknown> | undefined) ? (
                    <details><summary className="cursor-pointer text-sm font-medium">Build plan</summary><div className="mt-2"><PlanView plan={r.build_ref.plan as Record<string, unknown>} /></div></details>
                  ) : <p className="text-sm text-[var(--muted)]">Preparing the sandbox (fetching source and planning the build)…</p>}
                </div>
              </Card>
            )}
            {finished && r.kind !== "load" && <Card title="Results"><pre className="max-h-96 overflow-auto text-xs">{JSON.stringify(r.summary, null, 2)}</pre></Card>}
            {finished && (artifacts.data?.length ?? 0) > 0 && (
              <Card title="Artifacts">
                <ul className="space-y-1 text-sm">
                  {artifacts.data!.map((a) => (
                    <li key={a.id}><button className="text-[var(--accent)] underline" onClick={() => openArtifact(a.id)}>{a.kind} · {a.name}</button> <span className="text-xs text-[var(--muted)]">{(a.size_bytes / 1024).toFixed(0)} KB{a.sensitivity !== "normal" && " · may contain sensitive data"}</span></li>
                  ))}
                </ul>
              </Card>
            )}
            <ErrorNote error={stop.error} />
            <ConfirmDialog open={stopping} title="Stop this run?" confirmLabel="Stop now" danger busy={stop.isPending} onCancel={() => setStopping(false)} onConfirm={() => stop.mutate()}>
              <p>Traffic to the target stops within about 10 seconds. Partial results are kept.</p>
            </ConfirmDialog>
          </div>
        );
      }}
    </StateView>
  );
}
