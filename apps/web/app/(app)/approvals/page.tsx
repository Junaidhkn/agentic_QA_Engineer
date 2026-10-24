"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";

import { Badge, Button, Card, ErrorNote, fmtDate, PageHeader, StateView } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useOrg } from "@/lib/org";
import type { Approval } from "@/lib/types";

const HIDDEN = new Set(["project_id"]);

function show(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.map(show).join(", ") || "—";
  if (typeof v === "object") return Object.entries(v as Record<string, unknown>).map(([k, x]) => `${k} ${show(x)}`).join(" · ");
  return String(v);
}

function detailHref(a: Approval): string | null {
  const project = a.summary.project_id;
  if (a.subject_type === "repair_pr" && typeof project === "string") return `/projects/${project}/repairs/${a.subject_id}`;
  return null;
}

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
                  {Object.entries(a.summary).filter(([k]) => !HIDDEN.has(k)).map(([k, v]) => <div key={k}><dt className="inline text-[var(--muted)]">{k.replaceAll("_", " ")}: </dt><dd className="inline">{show(v)}</dd></div>)}
                </dl>
                {detailHref(a) && <p className="mt-2 text-sm"><Link className="underline" href={detailHref(a) ?? "#"}>Review the diff, gates and evidence</Link></p>}
                <p className="mt-2 text-xs text-[var(--muted)]">Expires {fmtDate(a.expires_at)}</p>
              </Card>
            ))}
          </div>
        )}
      </StateView>
    </>
  );
}
