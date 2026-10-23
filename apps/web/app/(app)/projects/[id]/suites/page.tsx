"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";

import { Badge, Button, Card, ErrorNote, Field, fmtDate, Select, StateView, Table } from "@/components/ui";
import { apiFetch, newIdempotencyKey } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { DownloadUrl, Suite, SuiteCase, Target } from "@/lib/types";

const BUSY = new Set(["generating", "validating"]);

function CaseDetail({ c }: { c: SuiteCase }) {
  const d = (c.validation_detail ?? {}) as { runs?: number; passed?: number; failure?: string | null; reason?: string; repaired?: boolean; repair_rejected?: string[] };
  return (
    <div className="space-y-1 text-xs">
      {d.runs !== undefined && <p>Dry runs passed: {d.passed}/{d.runs}</p>}
      {d.reason && <p className="text-[var(--muted)]">{d.reason}</p>}
      {d.repaired && <p>The workflow IR was repaired automatically for this suite; see the repaired IR below and apply it to the workflow if it is right.</p>}
      {d.repair_rejected && <p className="text-[var(--muted)]">Repair not applied: {d.repair_rejected.join("; ")}</p>}
      {d.failure && <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-black/5 p-2">{d.failure}</pre>}
    </div>
  );
}

export default function SuitesPage() {
  const { id, role } = useProject();
  const qc = useQueryClient();
  const engineer = hasRole(role, "engineer");
  const suites = useQuery({
    queryKey: ["suites", id],
    queryFn: () => apiFetch<Suite[]>(`/v1/projects/${id}/test-suites`),
    refetchInterval: (q) => ((q.state.data ?? []).some((s) => BUSY.has(s.status)) ? 3000 : false),
  });
  const targets = useQuery({ queryKey: ["targets", id], queryFn: () => apiFetch<Target[]>(`/v1/projects/${id}/targets`) });
  const verified = (targets.data ?? []).filter((t) => t.status === "verified");
  const [targetId, setTargetId] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: ["suite", open],
    queryFn: () => apiFetch<Suite>(`/v1/test-suites/${open}`),
    enabled: Boolean(open),
    refetchInterval: (q) => (q.state.data && BUSY.has(q.state.data.status) ? 3000 : false),
  });
  const generate = useMutation({
    mutationFn: () => apiFetch<Suite>(`/v1/projects/${id}/test-suites`, {
      method: "POST", idempotencyKey: newIdempotencyKey(), body: JSON.stringify({ validate_target_id: targetId || null }),
    }),
    onSuccess: (s) => { setOpen(s.id); qc.invalidateQueries({ queryKey: ["suites", id] }); },
  });
  const download = async (sid: string, part: string) => {
    const { url } = await apiFetch<DownloadUrl>(`/v1/test-suites/${sid}/bundle?part=${part}`);
    window.open(url, "_blank", "noopener");
  };
  const repaired = (detail.data?.generator_info as { repaired_irs?: Record<string, unknown> } | undefined)?.repaired_irs ?? {};

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="Suite versions">
        <p className="mb-3 text-sm text-[var(--muted)]">
          Tests are compiled deterministically from approved workflows: Playwright for browser workflows, k6 for APIs and load, k6 probes for
          concurrency invariants. With a validation target, each test is dry-run three times (1 virtual user, 1 iteration); failing tests get up to
          three automatic repairs of their workflow IR, and tests that still fail are marked invalid and excluded from runs.
        </p>
        {engineer && (
          <div className="mb-4 flex flex-wrap items-end gap-2">
            <Field label="Validate against" hint={verified.length ? undefined : "Verify a target to enable dry-run validation."}>
              <Select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                <option value="">No validation (compile only)</option>
                {verified.map((t) => <option key={t.id} value={t.id}>{t.base_url} ({t.environment})</option>)}
              </Select>
            </Field>
            <Button onClick={() => generate.mutate()} disabled={generate.isPending}>Generate from approved workflows</Button>
          </div>
        )}
        <ErrorNote error={generate.error} />
        <StateView query={suites} empty={<p>No suites yet.</p>}>
          {(list) => (
            <Table head={["Version", "Status", "Created", ""]}>
              {list.map((s) => (
                <tr key={s.id}>
                  <td className="px-2 py-2">v{s.version}</td>
                  <td className="px-2 py-2"><Badge>{s.status}</Badge></td>
                  <td className="px-2 py-2">{fmtDate(s.created_at)}</td>
                  <td className="px-2 py-2 text-right"><Button variant="ghost" onClick={() => setOpen(s.id)}>Details</Button></td>
                </tr>
              ))}
            </Table>
          )}
        </StateView>
      </Card>
      {open && (
        <Card title="Test cases" actions={engineer && detail.data && !BUSY.has(detail.data.status) && (
          <div className="flex gap-2"><Button variant="secondary" onClick={() => download(open, "k6")}>k6 bundle</Button><Button variant="secondary" onClick={() => download(open, "playwright")}>Playwright bundle</Button></div>
        )}>
          <StateView query={detail}>
            {(s) => (
              <div className="space-y-4">
                {BUSY.has(s.status) && <p className="text-sm"><Badge>{s.status}</Badge> {s.status === "validating" ? "Dry runs are in progress on a runner." : "Compiling tests…"}</p>}
                <Table head={["Workflow", "Type", "Validation", ""]}>
                  {(s.cases ?? []).map((c) => (
                    <Fragment key={c.id}>
                      <tr>
                        <td className="px-2 py-2 font-mono text-xs">{c.workflow_key}</td>
                        <td className="px-2 py-2">{c.type.replace("_", " ")}</td>
                        <td className="px-2 py-2"><Badge>{c.validation_status}</Badge></td>
                        <td className="px-2 py-2 text-right">{c.validation_detail && <Button variant="ghost" onClick={() => setExpanded(expanded === c.id ? null : c.id)}>{expanded === c.id ? "Hide" : "Why"}</Button>}</td>
                      </tr>
                      {expanded === c.id && <tr><td colSpan={4} className="px-4 pb-3"><CaseDetail c={c} /></td></tr>}
                    </Fragment>
                  ))}
                </Table>
                {Object.keys(repaired).length > 0 && (
                  <details className="text-xs">
                    <summary className="cursor-pointer text-sm font-medium">Repaired workflow IR ({Object.keys(repaired).length})</summary>
                    <pre className="mt-2 max-h-80 overflow-auto rounded bg-black/5 p-2">{JSON.stringify(repaired, null, 2)}</pre>
                  </details>
                )}
              </div>
            )}
          </StateView>
        </Card>
      )}
    </div>
  );
}
