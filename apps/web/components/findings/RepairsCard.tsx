"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { Badge, Card, fmtDate } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import type { Repair } from "@/lib/types";

const ACTIVE = new Set(["queued", "planning", "coding", "candidate", "verifying"]);

/** Repair attempts for a finding (newest first), linking to the diff, gates and approval. */
export function RepairsCard({ findingId, projectId }: { findingId: string; projectId: string }) {
  const repairs = useQuery({
    queryKey: ["repairs", findingId],
    queryFn: () => apiFetch<Repair[]>(`/v1/findings/${findingId}/repairs`),
    refetchInterval: (q) => ((q.state.data ?? []).some((r) => ACTIVE.has(r.status)) ? 4000 : false),
  });
  if (!repairs.data?.length) return null;
  return (
    <Card title={`Repairs (${repairs.data.length})`}>
      <ul className="divide-y divide-[var(--border)] text-sm">
        {repairs.data.map((r) => (
          <li key={r.id} className="flex flex-wrap items-center gap-2 py-2">
            <Link className="text-[var(--accent)]" href={`/projects/${projectId}/repairs/${r.id}`}>Attempt {r.attempt_no}</Link>
            <Badge>{r.status}</Badge>
            {r.diff_stats && <span className="font-mono text-xs text-[var(--muted)]">{String(r.diff_stats.files)} files +{String(r.diff_stats.additions)}/−{String(r.diff_stats.deletions)}</span>}
            <span className="ml-auto text-xs text-[var(--muted)]">{fmtDate(r.created_at)}</span>
            {r.status_reason && <p className="w-full text-xs text-[var(--muted)]">{r.status_reason}</p>}
          </li>
        ))}
      </ul>
    </Card>
  );
}
