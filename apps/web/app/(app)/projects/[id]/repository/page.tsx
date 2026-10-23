"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";

import { Button, Card, ErrorNote, StateView, Table } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { LinkedRepo, Repo } from "@/lib/types";

export default function RepositoryPage() {
  const { id, role } = useProject();
  const qc = useQueryClient();
  const linked = useQuery({ queryKey: ["repo", id], queryFn: () => apiFetch<LinkedRepo | null>(`/v1/projects/${id}/repository`) });
  const admin = hasRole(role, "admin");
  const repos = useQuery({ queryKey: ["gh-repos"], queryFn: () => apiFetch<Repo[]>("/v1/integrations/github/repositories"), enabled: admin && linked.data === null });
  const link = useMutation({
    mutationFn: (r: Repo) => apiFetch<LinkedRepo>(`/v1/projects/${id}/repository`, { method: "PUT", body: JSON.stringify({ installation_id: r.installation_id, github_repo_id: r.github_repo_id }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["repo", id] }),
  });
  return (
    <Card title="GitHub repository">
      <StateView query={linked}>
        {(repo) => repo ? (
          <p className="text-sm">Linked to <strong>{repo.full_name}</strong> (default branch <code>{repo.default_branch}</code>).</p>
        ) : !admin ? <p className="text-sm">No repository linked. Ask an admin to link one.</p> : (
          <div className="space-y-3">
            <p className="text-sm text-[var(--muted)]">Pick a repository from your GitHub App installations. Missing one? <Link className="text-[var(--accent)]" href="/settings/integrations">Install or configure the GitHub App</Link>.</p>
            <ErrorNote error={link.error} />
            <StateView query={repos} empty={<p>No repositories visible to the GitHub App yet.</p>}>
              {(list) => (
                <Table head={["Repository", "Default branch", ""]}>
                  {list.map((r) => (
                    <tr key={r.github_repo_id}>
                      <td className="px-2 py-2">{r.full_name}{r.private && " 🔒"}</td>
                      <td className="px-2 py-2">{r.default_branch}</td>
                      <td className="px-2 py-2 text-right"><Button variant="secondary" onClick={() => link.mutate(r)} disabled={link.isPending}>Link</Button></td>
                    </tr>
                  ))}
                </Table>
              )}
            </StateView>
          </div>
        )}
      </StateView>
    </Card>
  );
}
