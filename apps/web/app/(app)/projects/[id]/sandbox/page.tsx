"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { PlanView, ReportView } from "@/components/sandbox/SandboxDetails";
import { Badge, Button, Card, ErrorNote, Field, fmtDate, Input, StateView, Table } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { DownloadUrl, LinkedRepo, SandboxBuild, SourceArchive } from "@/lib/types";

const BUSY = new Set(["queued", "planning", "building"]);

export default function SandboxPage() {
  const { id, role } = useProject();
  const qc = useQueryClient();
  const engineer = hasRole(role, "engineer");
  const repo = useQuery({ queryKey: ["repo", id], queryFn: () => apiFetch<LinkedRepo | null>(`/v1/projects/${id}/repository`) });
  const builds = useQuery({
    queryKey: ["sandbox-builds", id],
    queryFn: () => apiFetch<SandboxBuild[]>(`/v1/projects/${id}/sandbox-builds`),
    refetchInterval: (q) => ((q.state.data ?? []).some((b) => BUSY.has(b.status)) ? 3000 : false),
  });
  const [ref, setRef] = useState("");
  const [archive, setArchive] = useState<File | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const start = useMutation({
    mutationFn: async () => {
      let archiveId: string | null = null;
      if (archive) {
        archiveId = (await apiFetch<SourceArchive>(`/v1/projects/${id}/source-archives`, {
          method: "POST", body: archive, headers: { "Content-Type": "application/octet-stream" },
        })).archive_artifact_id;
      }
      return apiFetch<SandboxBuild>(`/v1/projects/${id}/sandbox-builds`, {
        method: "POST", body: JSON.stringify({ commit_ref: ref || null, archive_artifact_id: archiveId }),
      });
    },
    onSuccess: (b) => { setOpen(b.id); qc.invalidateQueries({ queryKey: ["sandbox-builds", id] }); },
  });
  const log = async (bid: string) => {
    const { url } = await apiFetch<DownloadUrl>(`/v1/sandbox-builds/${bid}/log`);
    window.open(url, "_blank", "noopener");
  };
  const selected = (builds.data ?? []).find((b) => b.id === open) ?? builds.data?.[0];

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <div className="space-y-6">
        {engineer && (
          <Card title="Check that the app builds in a sandbox">
            <p className="mb-3 text-sm text-[var(--muted)]">
              Sandboxes build your app from source, start it with an ephemeral database (migrations and seed applied), point external
              integrations at a stub server, and collect traces and database statistics. Load tests and fix verification can then run
              against a controlled copy of your app instead of a shared environment. Some apps cannot be built this way (for example,
              apps that depend on hosted backends); use a preview URL target for those.
            </p>
            <div className="space-y-3">
              {repo.data ? (
                <Field label={`Branch or commit of ${repo.data.full_name}`} hint={`Default: ${repo.data.default_branch}`}>
                  <Input value={ref} onChange={(e) => setRef(e.target.value)} placeholder={repo.data.default_branch} />
                </Field>
              ) : (
                <Field label="Source archive (.zip or .tar.gz)">
                  <Input type="file" accept=".zip,.tar.gz,.tgz" onChange={(e) => setArchive(e.target.files?.[0] ?? null)} />
                </Field>
              )}
              <ErrorNote error={start.error} />
              <Button onClick={() => start.mutate()} disabled={start.isPending || (!repo.data && !archive)}>Build sandbox</Button>
            </div>
          </Card>
        )}
        <Card title="Builds">
          <StateView query={builds} empty={<p>No sandbox builds yet.</p>}>
            {(list) => (
              <Table head={["Started", "Status", "Commit", ""]}>
                {list.map((b) => (
                  <tr key={b.id}>
                    <td className="px-2 py-2">{fmtDate(b.created_at)}</td>
                    <td className="px-2 py-2"><Badge>{b.status}</Badge></td>
                    <td className="px-2 py-2 font-mono text-xs">{!b.commit_sha || /^0+$/.test(b.commit_sha) ? "archive" : b.commit_sha.slice(0, 10)}</td>
                    <td className="px-2 py-2 text-right"><Button variant="ghost" onClick={() => setOpen(b.id)}>Details</Button></td>
                  </tr>
                ))}
              </Table>
            )}
          </StateView>
        </Card>
      </div>
      {selected && (
        <div className="space-y-6">
          <Card title={<span className="flex items-center gap-2">Build plan <Badge>{selected.status}</Badge></span>}
            actions={engineer && selected.report && <Button variant="secondary" onClick={() => log(selected.id)}>Build log</Button>}>
            {selected.plan ? <PlanView plan={selected.plan} /> : <p className="text-sm text-[var(--muted)]">Planning…</p>}
            {selected.error && <p className="mt-3 text-sm text-[var(--danger)]">{selected.error}</p>}
          </Card>
          {selected.report && <Card title="Result"><ReportView report={selected.report} /></Card>}
        </div>
      )}
    </div>
  );
}
