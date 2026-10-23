"use client";

import { useQuery } from "@tanstack/react-query";

import { Badge, Card, StateView, TextLink } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useProject } from "@/lib/project";
import type { Target } from "@/lib/types";

export default function ProjectOverview() {
  const { id, project } = useProject();
  const targets = useQuery({ queryKey: ["targets", id], queryFn: () => apiFetch<Target[]>(`/v1/projects/${id}/targets`) });
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="Service-level objective">
        {project && <p className="text-sm">p95 latency &lt; <strong>{project.default_slo.p95_ms} ms</strong>, failed requests &lt; <strong>{(project.default_slo.error_rate * 100).toFixed(1)}%</strong>. Load profiles can override these.</p>}
      </Card>
      <Card title="Targets" actions={<TextLink href={`/projects/${id}/targets`}>Manage</TextLink>}>
        <StateView query={targets} empty={<p>No targets registered yet.</p>}>
          {(list) => <ul className="space-y-1 text-sm">{list.map((t) => <li key={t.id}>{t.host} <Badge>{t.status}</Badge> <Badge tone="info">{t.environment}</Badge></li>)}</ul>}
        </StateView>
      </Card>
    </div>
  );
}
