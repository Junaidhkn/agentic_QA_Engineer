"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Badge, Button, Card, ErrorNote, Field, fmtDate, Select, StateView, Table } from "@/components/ui";
import { apiFetch, newIdempotencyKey } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { Profile, Run, Target } from "@/lib/types";

export default function RunsPage() {
  const { id, role } = useProject();
  const qc = useQueryClient();
  const router = useRouter();
  const runs = useQuery({ queryKey: ["runs", id], queryFn: () => apiFetch<Run[]>(`/v1/projects/${id}/runs`), refetchInterval: 5000 });
  const targets = useQuery({ queryKey: ["targets", id], queryFn: () => apiFetch<Target[]>(`/v1/projects/${id}/targets`) });
  const profiles = useQuery({ queryKey: ["profiles", id], queryFn: () => apiFetch<Profile[]>(`/v1/projects/${id}/load-profiles`) });
  const [kind, setKind] = useState("functional");
  const [targetId, setTargetId] = useState("");
  const [profileId, setProfileId] = useState("");
  const [confirming, setConfirming] = useState(false);
  const verified = (targets.data ?? []).filter((t) => t.status === "verified");
  const target = verified.find((t) => t.id === targetId) ?? verified[0];
  const profile = (profiles.data ?? []).find((p) => p.id === profileId) ?? profiles.data?.[0];
  const start = useMutation({
    mutationFn: () => apiFetch<Run>(`/v1/projects/${id}/runs`, {
      method: "POST", idempotencyKey: newIdempotencyKey(),
      body: JSON.stringify({ kind, load_profile_id: kind === "load" ? profile?.id : undefined, environment: { type: "target_url", target_id: target?.id } }),
    }),
    onSuccess: (r) => { setConfirming(false); qc.invalidateQueries({ queryKey: ["runs", id] }); router.push(`/projects/${id}/runs/${r.id}`); },
  });
  const canRun = hasRole(role, "engineer") && target && (kind !== "load" || profile);

  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <Card title="Run history" className="lg:col-span-2">
        <StateView query={runs} empty={<p>No runs yet.</p>}>
          {(list) => (
            <Table head={["Run", "Kind", "Status", "p95", "Errors", "Started"]}>
              {list.map((r) => {
                const o = (r.summary?.overall ?? {}) as { p95_ms?: number; error_rate?: number };
                return (
                  <tr key={r.id}>
                    <td className="px-2 py-2"><Link className="font-mono text-xs text-[var(--accent)]" href={`/projects/${id}/runs/${r.id}`}>{r.id.slice(0, 8)}</Link></td>
                    <td className="px-2 py-2">{r.kind}{r.profile ? ` · ${String(r.profile.type)} ${String(r.profile.max_vus)} VUs` : ""}</td>
                    <td className="px-2 py-2"><Badge>{r.status}</Badge></td>
                    <td className="px-2 py-2">{o.p95_ms ? `${Math.round(o.p95_ms)} ms` : "—"}</td>
                    <td className="px-2 py-2">{o.error_rate !== undefined ? `${(o.error_rate * 100).toFixed(2)}%` : "—"}</td>
                    <td className="px-2 py-2">{fmtDate(r.started_at ?? r.created_at)}</td>
                  </tr>
                );
              })}
            </Table>
          )}
        </StateView>
      </Card>
      {hasRole(role, "engineer") && (
        <Card title="Start a run">
          <div className="space-y-3">
            <Field label="Kind">
              <Select value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="functional">Functional (Playwright + API smoke)</option>
                <option value="load">Load (k6)</option>
                <option value="probe">Concurrency probes (race conditions)</option>
              </Select>
            </Field>
            <Field label="Target" hint={verified.length ? undefined : "Verify a target first."}>
              <Select value={target?.id ?? ""} onChange={(e) => setTargetId(e.target.value)}>{verified.map((t) => <option key={t.id} value={t.id}>{t.host} ({t.environment})</option>)}</Select>
            </Field>
            {kind === "load" && (
              <Field label="Load profile">
                <Select value={profile?.id ?? ""} onChange={(e) => setProfileId(e.target.value)}>{(profiles.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name} ({p.type}, {p.max_vus} VUs)</option>)}</Select>
              </Field>
            )}
            <ErrorNote error={start.error} />
            <Button disabled={!canRun} onClick={() => setConfirming(true)}>Start…</Button>
          </div>
        </Card>
      )}
      <ConfirmDialog open={confirming} title="Start this run?" confirmLabel="Start run" busy={start.isPending} onCancel={() => setConfirming(false)} onConfirm={() => start.mutate()}>
        <p><strong>Target:</strong> {target?.host} ({target?.environment})</p>
        <p><strong>Kind:</strong> {kind}</p>
        {kind === "load" && profile && <p><strong>Load:</strong> {profile.type}, up to {profile.max_vus} virtual users for {Math.round(profile.duration_s / 60)} min</p>}
        <p className="text-[var(--muted)]">Traffic comes only from our published egress IPs and stops within seconds when you press Stop. First load tests and runs above 500 users need an admin&apos;s approval.</p>
      </ConfirmDialog>
    </div>
  );
}
