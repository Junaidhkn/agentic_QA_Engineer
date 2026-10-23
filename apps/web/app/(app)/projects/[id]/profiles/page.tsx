"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Badge, Button, Card, ErrorNote, Field, Input, Select, StateView, Table } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { Profile, Workflow } from "@/lib/types";

const TYPES = ["smoke", "baseline", "load", "stress", "spike", "soak"] as const;
const HINT: Record<string, string> = {
  smoke: "1–2 users for 30 s — is everything wired up?",
  baseline: "10% of peak for ~2.5 min — reference numbers for comparisons",
  load: "ramp to peak, hold 10 min — the launch-day check",
  stress: "step up 25/50/75/100% — find the knee where latency breaks",
  spike: "jump from 10% to 100% in 10 s — sudden traffic",
  soak: "60% of peak for up to an hour — leaks and slow degradation",
};

export default function ProfilesPage() {
  const { id, role, project } = useProject();
  const qc = useQueryClient();
  const profiles = useQuery({ queryKey: ["profiles", id], queryFn: () => apiFetch<Profile[]>(`/v1/projects/${id}/load-profiles`) });
  const workflows = useQuery({ queryKey: ["workflows", id, "approved"], queryFn: () => apiFetch<Workflow[]>(`/v1/projects/${id}/workflows?status=approved`) });
  const [form, setForm] = useState({ name: "", type: "load", max_vus: 100, p95_ms: "", error_rate: "" });
  const [weights, setWeights] = useState<Record<string, string>>({});
  const create = useMutation({
    mutationFn: () => {
      const mix: Record<string, number> = {};
      for (const [k, v] of Object.entries(weights)) if (Number(v) > 0) mix[k] = Number(v);
      const thresholds: Record<string, number> = {};
      if (form.p95_ms) thresholds.p95_ms = Number(form.p95_ms);
      if (form.error_rate) thresholds.error_rate = Number(form.error_rate) / 100;
      return apiFetch<Profile>(`/v1/projects/${id}/load-profiles`, { method: "POST", body: JSON.stringify({ name: form.name, type: form.type, max_vus: Number(form.max_vus), workload_mix: mix, thresholds }) });
    },
    onSuccess: () => { setForm({ ...form, name: "" }); qc.invalidateQueries({ queryKey: ["profiles", id] }); },
  });
  const remove = useMutation({ mutationFn: (pid: string) => apiFetch(`/v1/load-profiles/${pid}`, { method: "DELETE" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["profiles", id] }) });
  const engineer = hasRole(role, "engineer");
  const apiWorkflows = (workflows.data ?? []).filter((w) => w.kind === "api" || (w.spec.api_equivalent as unknown[] | undefined)?.length);

  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <Card title="Load profiles" className="lg:col-span-2">
        <StateView query={profiles} empty={<p>No profiles yet.</p>}>
          {(list) => (
            <Table head={["Name", "Type", "Peak VUs", "Duration", "Thresholds", ""]}>
              {list.map((p) => (
                <tr key={p.id}>
                  <td className="px-2 py-2 font-medium">{p.name}</td>
                  <td className="px-2 py-2"><Badge tone="info">{p.type}</Badge></td>
                  <td className="px-2 py-2">{p.max_vus}</td>
                  <td className="px-2 py-2">{Math.round(p.duration_s / 60)} min</td>
                  <td className="px-2 py-2 text-xs">p95 &lt; {p.thresholds.p95_ms} ms · errors &lt; {((p.thresholds.error_rate ?? 0) * 100).toFixed(1)}% · abort &gt; {((p.thresholds.abort_error_rate ?? 0.5) * 100).toFixed(0)}%</td>
                  <td className="px-2 py-2 text-right">{engineer && <Button variant="ghost" onClick={() => remove.mutate(p.id)}>Delete</Button>}</td>
                </tr>
              ))}
            </Table>
          )}
        </StateView>
      </Card>
      {engineer && (
        <Card title="New profile">
          <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); create.mutate(); }}>
            <Field label="Name"><Input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
            <Field label="Type" hint={HINT[form.type]}>
              <Select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>{TYPES.map((t) => <option key={t}>{t}</option>)}</Select>
            </Field>
            <Field label="Peak virtual users" hint="Up to 1,000 (plan limits apply).">
              <Input type="number" min={1} max={1000} value={form.max_vus} onChange={(e) => setForm({ ...form, max_vus: Number(e.target.value) })} />
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="p95 (ms)"><Input type="number" placeholder={String(project?.default_slo.p95_ms ?? 500)} value={form.p95_ms} onChange={(e) => setForm({ ...form, p95_ms: e.target.value })} /></Field>
              <Field label="Max errors (%)"><Input type="number" step="0.1" placeholder={String(((project?.default_slo.error_rate ?? 0.01) * 100).toFixed(1))} value={form.error_rate} onChange={(e) => setForm({ ...form, error_rate: e.target.value })} /></Field>
            </div>
            <fieldset className="space-y-1 text-sm">
              <legend className="font-medium">Workload mix (weights; empty = equal)</legend>
              {apiWorkflows.map((w) => (
                <label key={w.key} className="flex items-center justify-between gap-2">
                  <span className="truncate font-mono text-xs">{w.key}</span>
                  <Input className="w-20" type="number" min={0} step="0.1" value={weights[w.key] ?? ""} onChange={(e) => setWeights({ ...weights, [w.key]: e.target.value })} />
                </label>
              ))}
            </fieldset>
            <ErrorNote error={create.error} />
            <Button type="submit" disabled={create.isPending}>Create</Button>
          </form>
        </Card>
      )}
    </div>
  );
}
