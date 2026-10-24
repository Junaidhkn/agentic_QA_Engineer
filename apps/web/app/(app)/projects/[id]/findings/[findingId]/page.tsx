"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";

import { DiagnosisPanel } from "@/components/findings/DiagnosisPanel";
import { EvidenceCard } from "@/components/findings/EvidenceCard";
import { RepairsCard } from "@/components/findings/RepairsCard";
import { Badge, Button, Card, ErrorNote, fmtDate, StateView } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { Finding } from "@/lib/types";

export default function FindingDetail() {
  const { findingId } = useParams<{ findingId: string }>();
  const { id, role } = useProject();
  const qc = useQueryClient();
  const finding = useQuery({ queryKey: ["finding", findingId], queryFn: () => apiFetch<Finding>(`/v1/findings/${findingId}`) });
  const triage = useMutation({
    mutationFn: (status: string) => apiFetch(`/v1/findings/${findingId}`, { method: "PATCH", body: JSON.stringify({ status }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["finding", findingId] }),
  });
  return (
    <StateView query={finding}>
      {(f) => (
        <div className="space-y-6">
          <div>
            <p className="text-xs text-[var(--muted)]"><Link href={`/projects/${id}/findings`}>Findings</Link> /</p>
            <h2 className="text-xl font-semibold">{f.title}</h2>
            <p className="mt-1 flex flex-wrap items-center gap-2 text-sm"><Badge>{f.severity}</Badge><Badge>{f.status}</Badge><span className="text-[var(--muted)]">{f.type.replaceAll("_", " ")} · seen {f.occurrences}× · first {fmtDate(f.created_at)} · <Link className="text-[var(--accent)]" href={`/projects/${id}/runs/${f.last_run_id}`}>latest run</Link></span></p>
          </div>
          <Card title="What was measured">
            <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
              {Object.entries(f.headline).map(([k, v]) => <div key={k}><dt className="inline text-[var(--muted)]">{k.replaceAll("_", " ")}: </dt><dd className="inline">{typeof v === "number" ? Math.round(v * 1000) / 1000 : String(v ?? "—")}</dd></div>)}
            </dl>
          </Card>
          <Card title={`Evidence (${f.evidence?.length ?? 0})`}>
            <div className="space-y-3">{(f.evidence ?? []).map((e, i) => <EvidenceCard key={e.id} e={e} index={i} />)}</div>
          </Card>
          <DiagnosisPanel findingId={findingId} canRun={hasRole(role, "engineer")} projectId={id} />
          <RepairsCard findingId={findingId} projectId={id} />
          {hasRole(role, "engineer") && (
            <Card title="Triage">
              <ErrorNote error={triage.error} />
              <div className="flex flex-wrap gap-2">
                {["open", "wont_fix", "duplicate", "resolved"].filter((s) => s !== f.status).map((s) => <Button key={s} variant="secondary" onClick={() => triage.mutate(s)}>Mark {s.replace("_", " ")}</Button>)}
              </div>
            </Card>
          )}
        </div>
      )}
    </StateView>
  );
}
