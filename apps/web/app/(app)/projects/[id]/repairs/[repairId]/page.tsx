"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { DiffViewer } from "@/components/repairs/DiffViewer";
import { Badge, Button, Card, ErrorNote, Field, Input, StateView, Table, fmtDate } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { Comparison, Gate, RepairDetail } from "@/lib/types";

const ACTIVE = new Set(["queued", "planning", "coding", "candidate", "verifying"]);
const STEPS = [
  ["planning", "Plan"],
  ["coding", "Regression test + fix"],
  ["verifying", "Independent verification"],
  ["awaiting_approval", "Your review"],
  ["pr_opened", "Pull request"],
] as const;
const ORDER: Record<string, number> = {
  queued: 0, planning: 0, coding: 1, candidate: 2, verifying: 2, verified: 3, awaiting_approval: 3, approved: 4, pr_opened: 4,
};

function gateDetail(g: Gate): string {
  const r = (g.result ?? {}) as Record<string, unknown>;
  if (g.gate === "fail_to_pass" && Array.isArray(r.base_exit_codes)) {
    return `fails on base (exit ${(r.base_exit_codes as number[]).join(", ")}), passes with the fix (exit ${(r.patched_exit_codes as number[]).join(", ")})`;
  }
  if (g.gate === "diff_policy" && r.stats) {
    const s = r.stats as Record<string, number>;
    const v = (r.violations as string[] | undefined) ?? [];
    return v.length ? v.join("; ") : `${s.files} files, +${s.additions}/−${s.deletions}, within limits`;
  }
  if (g.gate === "security") {
    const blocking = (r.blocking as Array<Record<string, string>> | undefined) ?? [];
    return blocking.length
      ? `blocking: ${blocking.map((b) => `${b.rule} in ${b.path}`).join(", ")}`
      : `no new high-severity findings (${((r.scanners as string[] | undefined) ?? []).join(", ")})`;
  }
  if (g.gate === "load_comparison" && r.verdict) {
    const ci = (r.ci as number[] | undefined) ?? [];
    const latency = `${String(r.verdict)}: ${Number(r.delta_pct).toFixed(1)}% p95 (95% CI ${ci.map((x) => `${x.toFixed(1)}%`).join(" … ")}, noise ±${Number(r.noise_floor_pct).toFixed(1)}%)`;
    const rates = r.error_rates as number[] | undefined;
    if (r.error_rate_better && rates) {
      return `errors ${(rates[0] * 100).toFixed(2)}% → ${(rates[1] * 100).toFixed(2)}% (significant); ${latency}` +
        (Number(r.latency_cost_ms) > 0 ? `; +${Number(r.latency_cost_ms).toFixed(1)} ms p95 ${g.status === "passed" ? "within" : "over"} the ${Number(r.latency_budget_ms).toFixed(0)} ms budget for error fixes` : "");
    }
    return latency;
  }
  if (g.gate === "invariant_probe" && r.passed_runs !== undefined) return `${String(r.passed_runs)}/${String(r.of)} probe runs held the invariant`;
  if (g.gate === "functional_suite" && r.run_status) return `suite run ${String(r.run_status)}`;
  return String(r.reason ?? r.note ?? r.error ?? "");
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/x-diff" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

export default function RepairPage() {
  const { repairId } = useParams<{ repairId: string }>();
  const { id, role } = useProject();
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const repair = useQuery({
    queryKey: ["repair", repairId],
    queryFn: () => apiFetch<RepairDetail>(`/v1/repairs/${repairId}`),
    refetchInterval: (q) => (q.state.data && ACTIVE.has(q.state.data.status) ? 3000 : false),
  });
  const decide = useMutation({
    mutationFn: (decision: "approved" | "rejected") =>
      apiFetch(`/v1/approvals/${repair.data?.approval?.id}:decide`, { method: "POST", body: JSON.stringify({ decision, reason: reason || null }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["repair", repairId] }),
  });
  const cancel = useMutation({
    mutationFn: () => apiFetch(`/v1/repairs/${repairId}:cancel`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["repair", repairId] }),
  });
  return (
    <StateView query={repair}>
      {(r) => {
        const step = ORDER[r.status] ?? -1;
        const gates = r.verification?.gates ?? [];
        const engineer = hasRole(role, "engineer");
        return (
          <div className="space-y-6">
            <div>
              <p className="text-xs text-[var(--muted)]">
                <Link href={`/projects/${id}/findings`}>Findings</Link> / <Link href={`/projects/${id}/findings/${r.finding_id}`}>finding</Link> /
              </p>
              <h2 className="text-xl font-semibold">Repair attempt {r.attempt_no}</h2>
              <p className="mt-1 flex flex-wrap items-center gap-2 text-sm">
                <Badge>{r.status}</Badge>
                {r.status_reason && <span className="text-[var(--muted)]">{r.status_reason}</span>}
                <span className="text-[var(--muted)]">· started {fmtDate(r.created_at)}</span>
              </p>
              <ol className="mt-3 flex flex-wrap gap-2 text-xs" aria-label="Progress">
                {STEPS.map(([key, label], i) => (
                  <li key={key} className={i <= step ? "rounded bg-[var(--accent)]/10 px-2 py-1 font-medium" : "rounded px-2 py-1 text-[var(--muted)]"} aria-current={i === step ? "step" : undefined}>
                    {i + 1}. {label}
                  </li>
                ))}
              </ol>
            </div>

            {r.status === "awaiting_approval" && r.approval && !r.approval.decision && (
              <Card title="Review and approve">
                <p className="mb-3 text-sm">Every applicable verification gate passed. Approving opens a pull request on a <code>reliability/</code> branch; nothing is merged automatically.</p>
                <ErrorNote error={decide.error} />
                {engineer ? (
                  <div className="flex flex-wrap items-end gap-2">
                    <Field label="Note (optional)"><Input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} /></Field>
                    <Button disabled={decide.isPending} onClick={() => decide.mutate("approved")}>Approve and open PR</Button>
                    <Button variant="secondary" disabled={decide.isPending} onClick={() => decide.mutate("rejected")}>Reject</Button>
                  </div>
                ) : (
                  <p className="text-sm text-[var(--muted)]">An engineer or admin can approve this change. Expires {fmtDate(r.approval.expires_at)}.</p>
                )}
              </Card>
            )}

            {r.pull_request && (
              <Card title="Pull request">
                <p className="text-sm">
                  <Badge>{r.pull_request.state}</Badge>{" "}
                  {r.pull_request.url ? <a className="text-[var(--accent)] underline" href={r.pull_request.url} target="_blank" rel="noreferrer">#{r.pull_request.number} on {r.pull_request.branch}</a> : <span className="font-mono">{r.pull_request.branch}</span>}
                  {r.pull_request.error && <span className="text-[var(--danger)]"> — {r.pull_request.error}</span>}
                </p>
              </Card>
            )}

            {gates.length > 0 && (
              <Card title="Verification gates">
                <Table head={["Gate", "Result", "Details"]}>
                  {gates.map((g) => (
                    <tr key={g.gate} className="align-top">
                      <td className="px-2 py-1.5 capitalize">{g.gate.replaceAll("_", " ")}</td>
                      <td className="px-2 py-1.5">{g.applicable ? <Badge>{g.status}</Badge> : <span className="text-xs text-[var(--muted)]">not applicable</span>}</td>
                      <td className="px-2 py-1.5 text-xs">{gateDetail(g)}</td>
                    </tr>
                  ))}
                </Table>
                <p className="mt-2 text-xs text-[var(--muted)]">Verified means every applicable gate passed. The pipeline is deterministic and independent of the agent that wrote the change.</p>
              </Card>
            )}

            {(r.verification?.comparisons ?? []).length > 0 && (
              <Card title="Before / after (sandbox, interleaved runs)">
                <Table head={["Scope", "Before p95", "After p95", "Change", "95% CI", "Noise floor", "Verdict"]}>
                  {(r.verification?.comparisons ?? []).map((c: Comparison) => (
                    <tr key={c.scope}>
                      <td className="px-2 py-1.5 font-mono text-xs">{c.scope.replace("endpoint:", "")}</td>
                      <td className="px-2 py-1.5">{c.baseline_value.toFixed(1)} ms</td>
                      <td className="px-2 py-1.5">{c.candidate_value.toFixed(1)} ms</td>
                      <td className="px-2 py-1.5">{c.delta_pct > 0 ? "+" : ""}{c.delta_pct.toFixed(1)}%</td>
                      <td className="px-2 py-1.5 text-xs">[{c.ci_low_pct.toFixed(1)}%, {c.ci_high_pct.toFixed(1)}%]</td>
                      <td className="px-2 py-1.5 text-xs">±{c.noise_floor_pct.toFixed(1)}%</td>
                      <td className="px-2 py-1.5"><Badge>{c.verdict}</Badge></td>
                    </tr>
                  ))}
                </Table>
                <p className="mt-2 text-xs text-[var(--muted)]">Relative change of the median per-run p95; BCa bootstrap interval with requests resampled within each run ({r.verification?.comparisons[0]?.baseline_run_ids.length} runs per side).</p>
              </Card>
            )}

            {r.agent_summary && (
              <Card title={r.agent_summary_label}>
                <p className="whitespace-pre-wrap text-sm">{r.agent_summary}</p>
              </Card>
            )}

            {r.diff && (
              <Card title="Change" actions={<Button variant="secondary" onClick={() => download(`repair-${r.id.slice(0, 8)}.diff`, r.diff ?? "")}>Download patch</Button>}>
                <DiffViewer diff={r.diff} highlight={r.regression_test_paths ?? []} />
              </Card>
            )}

            {engineer && ACTIVE.has(r.status) && (
              <div><Button variant="ghost" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Cancel this repair</Button><ErrorNote error={cancel.error} /></div>
            )}
          </div>
        );
      }}
    </StateView>
  );
}
