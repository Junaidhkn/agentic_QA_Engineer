"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Badge, Button, Card, ErrorNote, Field, fmtDate, Input, Select, StateView, Table } from "@/components/ui";
import { apiFetch, newIdempotencyKey } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { Report, ReportShare } from "@/lib/types";

const SECTIONS = [
  ["runs", "Runs"],
  ["trends", "Latency trends"],
  ["coverage", "Workflow coverage"],
  ["findings", "Findings"],
  ["repairs", "Repairs"],
] as const;

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function period(r: Report): string {
  const p = r.params as { from?: string; to?: string };
  return p.from && p.to ? `${p.from.slice(0, 10)} → ${p.to.slice(0, 10)}` : "–";
}

export default function ReportsPage() {
  const { id, role } = useProject();
  const qc = useQueryClient();
  const engineer = hasRole(role, "engineer");
  const today = new Date();
  const [from, setFrom] = useState(isoDay(new Date(today.getTime() - 30 * 86400_000)));
  const [to, setTo] = useState(isoDay(today));
  const [sections, setSections] = useState<Set<string>>(new Set(SECTIONS.map(([k]) => k)));
  const [days, setDays] = useState("7");
  const [shared, setShared] = useState<{ reportId: string; share: ReportShare } | null>(null);
  const [revoking, setRevoking] = useState<Report | null>(null);

  const reports = useQuery({
    queryKey: ["reports", id],
    queryFn: () => apiFetch<Report[]>(`/v1/projects/${id}/reports`),
    refetchInterval: (q) => ((q.state.data ?? []).some((r) => r.status === "queued" || r.status === "generating") ? 2000 : false),
  });
  const create = useMutation({
    mutationFn: () =>
      apiFetch<Report>(`/v1/projects/${id}/reports`, {
        method: "POST",
        idempotencyKey: newIdempotencyKey(),
        body: JSON.stringify({
          from: new Date(`${from}T00:00:00Z`).toISOString(),
          to: new Date(`${to}T23:59:59Z`).toISOString(),
          sections: SECTIONS.map(([k]) => k).filter((k) => sections.has(k)),
        }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["reports", id] }),
  });
  const open = useMutation({
    mutationFn: async ({ reportId, kind }: { reportId: string; kind: "html" | "json" }) => {
      const r = await apiFetch<Report>(`/v1/reports/${reportId}`);
      const url = kind === "html" ? r.html_url : r.json_url;
      if (url) window.open(url, "_blank", "noopener");
    },
  });
  const share = useMutation({
    mutationFn: (reportId: string) =>
      apiFetch<ReportShare>(`/v1/reports/${reportId}/share`, { method: "POST", body: JSON.stringify({ expires_in_days: Number(days) }) }),
    onSuccess: (s, reportId) => { setShared({ reportId, share: s }); qc.invalidateQueries({ queryKey: ["reports", id] }); },
  });
  const revoke = useMutation({
    mutationFn: (reportId: string) => apiFetch<void>(`/v1/reports/${reportId}/share`, { method: "DELETE" }),
    onSuccess: () => { setRevoking(null); setShared(null); qc.invalidateQueries({ queryKey: ["reports", id] }); },
  });

  return (
    <div className="space-y-6">
      <Card title="Generate a report">
        <p className="mb-4 text-sm text-[var(--muted)]">
          A point-in-time summary of this project: runs and their verdicts, steady-state p95 trends, workflow coverage, open findings and
          repairs. You get a self-contained HTML page (no scripts, safe to archive or attach) and the same data as JSON
          (<code>report@1</code>). Experiment, verification and noise-floor runs are left out.
        </p>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="From"><Input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} /></Field>
          <Field label="To"><Input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} /></Field>
          <fieldset className="sm:col-span-2">
            <legend className="text-sm font-medium">Sections</legend>
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm">
              {SECTIONS.map(([k, label]) => (
                <label key={k} className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={sections.has(k)}
                    onChange={() => setSections((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; })}
                  />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>
        </div>
        <div className="mt-4 space-y-2">
          <ErrorNote error={create.error} />
          <Button onClick={() => create.mutate()} disabled={create.isPending || !from || !to}>
            {create.isPending ? "Queuing…" : "Generate report"}
          </Button>
        </div>
      </Card>

      {shared && (
        <Card title="Share link created">
          <p className="mb-2 text-sm">
            Anyone with this link can read the report until {fmtDate(shared.share.expires_at)}. It is shown only once; revoke it from the list
            below at any time.
          </p>
          <div className="flex gap-2">
            <Input readOnly value={shared.share.url} aria-label="Share link" onFocus={(e) => e.currentTarget.select()} />
            <Button variant="secondary" onClick={() => void navigator.clipboard?.writeText(shared.share.url)}>Copy</Button>
          </div>
        </Card>
      )}

      <Card
        title="Reports"
        actions={engineer && (
          <label className="flex items-center gap-2 text-sm text-[var(--muted)]">
            Share links expire after
            <Select value={days} onChange={(e) => setDays(e.target.value)} aria-label="Share link lifetime">
              {["1", "7", "14", "30"].map((d) => <option key={d} value={d}>{d} day{d === "1" ? "" : "s"}</option>)}
            </Select>
          </label>
        )}
      >
        <ErrorNote error={open.error ?? share.error} />
        <StateView query={reports} empty={<p className="text-sm text-[var(--muted)]">No reports yet.</p>}>
          {(list) => (
            <Table head={["Created", "Period", "Status", "Sharing", ""]}>
              {list.map((r) => (
                <tr key={r.id}>
                  <td className="px-2 py-2">{fmtDate(r.created_at)}</td>
                  <td className="px-2 py-2 font-mono text-xs">{period(r)}</td>
                  <td className="px-2 py-2"><Badge>{r.status}</Badge></td>
                  <td className="px-2 py-2 text-xs">{r.shared ? `until ${fmtDate(r.share_expires_at)}` : <span className="text-[var(--muted)]">private</span>}</td>
                  <td className="space-x-1 px-2 py-2 text-right whitespace-nowrap">
                    {r.status === "ready" && (
                      <>
                        <Button variant="ghost" onClick={() => open.mutate({ reportId: r.id, kind: "html" })}>HTML</Button>
                        <Button variant="ghost" onClick={() => open.mutate({ reportId: r.id, kind: "json" })}>JSON</Button>
                        {engineer && (
                          <Button variant="ghost" onClick={() => share.mutate(r.id)} disabled={share.isPending}>
                            {r.shared ? "New link" : "Share"}
                          </Button>
                        )}
                        {engineer && r.shared && <Button variant="ghost" onClick={() => setRevoking(r)}>Revoke</Button>}
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </Table>
          )}
        </StateView>
      </Card>

      <ConfirmDialog
        open={revoking !== null}
        title="Revoke share link?"
        confirmLabel="Revoke link"
        danger
        busy={revoke.isPending}
        onConfirm={() => revoking && revoke.mutate(revoking.id)}
        onCancel={() => setRevoking(null)}
      >
        <p className="text-sm">The current link stops working immediately for everyone who has it. The report itself is kept.</p>
        <ErrorNote error={revoke.error} />
      </ConfirmDialog>
    </div>
  );
}
