"use client";

import { useInfiniteQuery } from "@tanstack/react-query";

import { Button, Card, ErrorNote, fmtDate, Table } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole, useOrg } from "@/lib/org";
import type { AuditPage } from "@/lib/types";

export default function AuditPageView() {
  const { current } = useOrg();
  const q = useInfiniteQuery({
    queryKey: ["audit", current?.id],
    queryFn: ({ pageParam }) => apiFetch<AuditPage>(`/v1/orgs/${current?.id}/audit-events?limit=50${pageParam ? `&cursor=${pageParam}` : ""}`),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: hasRole(current?.role, "admin"),
  });
  if (!hasRole(current?.role, "admin")) return <p className="text-sm">Only admins can view the audit log.</p>;
  const rows = q.data?.pages.flatMap((p) => p.data) ?? [];
  return (
    <Card title="Audit log">
      <ErrorNote error={q.error} />
      <Table head={["Time", "Actor", "Action", "Subject", "Details"]}>
        {rows.map((e) => (
          <tr key={e.seq}>
            <td className="whitespace-nowrap px-2 py-2">{fmtDate(e.created_at)}</td>
            <td className="px-2 py-2">{e.actor_type}:{e.actor_id.slice(0, 8)}</td>
            <td className="px-2 py-2 font-mono text-xs">{e.action}</td>
            <td className="px-2 py-2 text-xs">{e.subject_type} {e.subject_id?.slice(0, 8)}</td>
            <td className="max-w-xs truncate px-2 py-2 text-xs">{JSON.stringify(e.metadata)}</td>
          </tr>
        ))}
      </Table>
      {q.hasNextPage && <Button variant="secondary" className="mt-3" onClick={() => q.fetchNextPage()} disabled={q.isFetchingNextPage}>Load more</Button>}
    </Card>
  );
}
