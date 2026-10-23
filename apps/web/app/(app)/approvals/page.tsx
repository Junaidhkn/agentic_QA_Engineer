"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Badge, Button, Card, ErrorNote, fmtDate, PageHeader, StateView } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useOrg } from "@/lib/org";
import type { Approval } from "@/lib/types";

export default function ApprovalsPage() {
  const { current } = useOrg();
  const qc = useQueryClient();
  const approvals = useQuery({ queryKey: ["approvals", current?.id], queryFn: () => apiFetch<Approval[]>("/v1/approvals"), enabled: Boolean(current) });
  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: string }) => apiFetch(`/v1/approvals/${id}:decide`, { method: "POST", body: JSON.stringify({ decision }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["approvals"] }),
  });
  return (
    <>
      <PageHeader title="Approvals" description="Nothing risky happens without a person saying yes: first load tests, large runs, production checks and pull requests." />
      <ErrorNote error={decide.error} />
      <StateView query={approvals} empty={<p>Nothing waiting for approval.</p>}>
        {(list) => (
          <div className="space-y-3">
            {list.map((a) => (
              <Card key={a.id} title={<span><Badge tone="warn">{a.subject_type.replace("_", " ")}</Badge> requested {fmtDate(a.created_at)}</span>}
                actions={<div className="flex gap-2"><Button onClick={() => decide.mutate({ id: a.id, decision: "approved" })}>Approve</Button><Button variant="secondary" onClick={() => decide.mutate({ id: a.id, decision: "rejected" })}>Reject</Button></div>}>
                <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
                  {Object.entries(a.summary).map(([k, v]) => <div key={k}><dt className="inline text-[var(--muted)]">{k.replace("_", " ")}: </dt><dd className="inline">{Array.isArray(v) ? v.join(", ") : String(v ?? "—")}</dd></div>)}
                </dl>
                <p className="mt-2 text-xs text-[var(--muted)]">Expires {fmtDate(a.expires_at)}</p>
              </Card>
            ))}
          </div>
        )}
      </StateView>
    </>
  );
}
