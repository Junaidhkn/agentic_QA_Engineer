"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Badge, Button, Card, ErrorNote, Field, fmtDate, Input, Select, StateView } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { IngestTokenCreated, Target } from "@/lib/types";

function VerificationHelp({ t }: { t: Target }) {
  const v = t.verification as { dns_txt: { name: string; value: string }; http_file: { url: string; content: string } };
  return (
    <div className="mt-3 space-y-2 rounded-md bg-black/5 p-3 text-xs">
      <p className="font-medium">Prove you control {t.host} (either option):</p>
      <p>1. DNS TXT record <code className="break-all">{v.dns_txt.name}</code> with value <code className="break-all">{v.dns_txt.value}</code></p>
      <p>2. Serve <code className="break-all">{v.http_file.url}</code> containing <code className="break-all">{v.http_file.content}</code></p>
      {t.egress_ips.length > 0 && <p>Test traffic comes from: {t.egress_ips.join(", ")} — allowlist these in your WAF.</p>}
    </div>
  );
}

export default function TargetsPage() {
  const { id, role } = useProject();
  const qc = useQueryClient();
  const targets = useQuery({ queryKey: ["targets", id], queryFn: () => apiFetch<Target[]>(`/v1/projects/${id}/targets`) });
  const [url, setUrl] = useState("");
  const [env, setEnv] = useState("staging");
  const [revoking, setRevoking] = useState<Target | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["targets", id] });
  const create = useMutation({
    mutationFn: () => apiFetch<Target>(`/v1/projects/${id}/targets`, { method: "POST", body: JSON.stringify({ base_url: url, environment: env }) }),
    onSuccess: () => { setUrl(""); refresh(); },
  });
  const verify = useMutation({ mutationFn: (tid: string) => apiFetch<Target>(`/v1/targets/${tid}:verify`, { method: "POST" }), onSettled: refresh });
  const revoke = useMutation({ mutationFn: (tid: string) => apiFetch<void>(`/v1/targets/${tid}`, { method: "DELETE" }), onSuccess: () => { setRevoking(null); refresh(); } });
  const admin = hasRole(role, "admin");

  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <div className="space-y-3 lg:col-span-2">
        <ErrorNote error={verify.error} />
        <StateView query={targets} empty={<p>No targets yet. Register the staging URL you want to test.</p>}>
          {(list) => list.map((t) => (
            <Card key={t.id} title={<span>{t.host} <Badge>{t.status}</Badge> <Badge tone="info">{t.environment}</Badge></span>}
              actions={admin && (
                <div className="flex gap-2">
                  <Button variant="secondary" onClick={() => verify.mutate(t.id)} disabled={verify.isPending}>Verify now</Button>
                  <Button variant="ghost" onClick={() => setRevoking(t)}>Revoke</Button>
                </div>
              )}>
              <p className="text-sm text-[var(--muted)]">{t.base_url} · verified {fmtDate(t.verified_at)} · expires {fmtDate(t.verification_expires_at)}</p>
              <p className="mt-1 text-xs text-[var(--muted)]">Allowed profiles: {(t.environment_policy.profiles as string[]).join(", ")}</p>
              {t.last_check_detail && t.status !== "verified" && <p className="mt-1 text-xs">Last check: {JSON.stringify(t.last_check_detail)}</p>}
              {t.status !== "verified" && <VerificationHelp t={t} />}
            </Card>
          ))}
        </StateView>
      </div>
      {admin && (
        <Card title="Register target">
          <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); create.mutate(); }}>
            <Field label="Base URL"><Input required type="url" placeholder="https://staging.example.com" value={url} onChange={(e) => setUrl(e.target.value)} /></Field>
            <Field label="Environment" hint="Production targets only allow small, approved smoke runs.">
              <Select value={env} onChange={(e) => setEnv(e.target.value)}>
                {["staging", "preview", "sandbox", "production"].map((x) => <option key={x}>{x}</option>)}
              </Select>
            </Field>
            <ErrorNote error={create.error} />
            <Button type="submit" disabled={create.isPending}>Register</Button>
          </form>
        </Card>
      )}
      {admin && <div className="lg:col-span-3"><TelemetrySetup projectId={id} /></div>}
      <ConfirmDialog open={revoking !== null} title={`Revoke ${revoking?.host}?`} confirmLabel="Revoke" danger busy={revoke.isPending}
        onCancel={() => setRevoking(null)} onConfirm={() => revoking && revoke.mutate(revoking.id)}>
        <p>All runs against this host stop within seconds and no new runs can start until it is verified again.</p>
      </ConfirmDialog>
    </div>
  );
}

function TelemetrySetup({ projectId }: { projectId: string }) {
  const [created, setCreated] = useState<IngestTokenCreated | null>(null);
  const create = useMutation({
    mutationFn: () => apiFetch<IngestTokenCreated>(`/v1/projects/${projectId}/ingest-tokens`, { method: "POST", body: JSON.stringify({ name: "staging" }) }),
    onSuccess: setCreated,
  });
  return (
    <Card title="Server-side telemetry (optional)">
      <p className="mb-2 text-sm text-[var(--muted)]">Diagnosis is much better when we can see traces and logs from your staging app. Point its OpenTelemetry exporter at us with an ingest token; data is stored per organization and kept 30 days.</p>
      {created ? (
        <pre className="overflow-auto rounded bg-black/5 p-2 text-xs">{Object.entries(created.env_example).map(([k, v]) => `${k}=${v}`).join("\n")}</pre>
      ) : <Button variant="secondary" onClick={() => create.mutate()} disabled={create.isPending}>Create ingest token</Button>}
      <ErrorNote error={create.error} />
    </Card>
  );
}
