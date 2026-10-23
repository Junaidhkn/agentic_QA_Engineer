"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge, Button, Card, ErrorNote, fmtDate, StateView, Table } from "@/components/ui";
import { apiFetch, newIdempotencyKey } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { DownloadUrl, Suite } from "@/lib/types";

export default function SuitesPage() {
  const { id, role } = useProject();
  const qc = useQueryClient();
  const suites = useQuery({ queryKey: ["suites", id], queryFn: () => apiFetch<Suite[]>(`/v1/projects/${id}/test-suites`) });
  const [open, setOpen] = useState<string | null>(null);
  const detail = useQuery({ queryKey: ["suite", open], queryFn: () => apiFetch<Suite>(`/v1/test-suites/${open}`), enabled: Boolean(open) });
  const generate = useMutation({
    mutationFn: () => apiFetch<Suite>(`/v1/projects/${id}/test-suites`, { method: "POST", body: "{}", idempotencyKey: newIdempotencyKey() }),
    onSuccess: (s) => { setOpen(s.id); qc.invalidateQueries({ queryKey: ["suites", id] }); },
  });
  const download = async (sid: string, part: string) => {
    const { url } = await apiFetch<DownloadUrl>(`/v1/test-suites/${sid}/bundle?part=${part}`);
    window.open(url, "_blank", "noopener");
  };
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="Suite versions" actions={hasRole(role, "engineer") && <Button onClick={() => generate.mutate()} disabled={generate.isPending}>Generate from approved workflows</Button>}>
        <p className="mb-3 text-sm text-[var(--muted)]">Tests are compiled deterministically from approved workflows: Playwright for browser workflows, k6 for APIs and load, k6 probes for concurrency invariants.</p>
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
        <Card title="Test cases" actions={hasRole(role, "engineer") && (
          <div className="flex gap-2"><Button variant="secondary" onClick={() => download(open, "k6")}>k6 bundle</Button><Button variant="secondary" onClick={() => download(open, "playwright")}>Playwright bundle</Button></div>
        )}>
          <StateView query={detail}>
            {(s) => (
              <Table head={["Workflow", "Type", "Validation"]}>
                {(s.cases ?? []).map((c) => (
                  <tr key={c.id}>
                    <td className="px-2 py-2 font-mono text-xs">{c.workflow_key}</td>
                    <td className="px-2 py-2">{c.type.replace("_", " ")}</td>
                    <td className="px-2 py-2"><Badge>{c.validation_status}</Badge></td>
                  </tr>
                ))}
              </Table>
            )}
          </StateView>
        </Card>
      )}
    </div>
  );
}
