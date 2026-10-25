"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { Badge, Card, fmtDate, Select, StateView, Table } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useProject } from "@/lib/project";
import type { Finding } from "@/lib/types";

export default function FindingsPage() {
  const { id } = useProject();
  const [status, setStatus] = useState("active");
  const findings = useQuery({ queryKey: ["findings", id, status], queryFn: () => apiFetch<Finding[]>(`/v1/projects/${id}/findings${status ? `?status=${status}` : ""}`) });
  return (
    <Card title="Findings" actions={
      <label className="text-sm">Show{" "}
        <Select className="inline-block w-36" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="active">Active</option><option value="resolved">Resolved</option><option value="">All</option>
        </Select>
      </label>}>
      <p className="mb-3 text-sm text-[var(--muted)]">Findings are created by deterministic rules from run results — each links the evidence it is based on.</p>
      <StateView query={findings} empty={<p>No findings. Run functional, load and probe tests to look for problems.</p>}>
        {(list) => (
          <Table head={["Severity", "Finding", "Type", "Seen", "Last seen", "Status"]}>
            {list.map((f) => (
              <tr key={f.id}>
                <td className="px-2 py-2"><Badge>{f.severity}</Badge></td>
                <td className="px-2 py-2"><Link className="text-[var(--accent)]" href={`/projects/${id}/findings/${f.id}`}>{f.title}</Link></td>
                <td className="px-2 py-2">{f.type.replaceAll("_", " ")}</td>
                <td className="px-2 py-2">{f.occurrences}×</td>
                <td className="px-2 py-2">{fmtDate(f.updated_at)}</td>
                <td className="px-2 py-2"><Badge>{f.status}</Badge></td>
              </tr>
            ))}
          </Table>
        )}
      </StateView>
    </Card>
  );
}
