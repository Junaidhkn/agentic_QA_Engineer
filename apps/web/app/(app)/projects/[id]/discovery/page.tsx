"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { Badge, Button, Card, ErrorNote, Field, fmtDate, Input, Select, StateView, Table } from "@/components/ui";
import { apiFetch, newIdempotencyKey } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { Discovery, LinkedRepo, OpenApiSpec, SecretMeta, SourceArchive, Target } from "@/lib/types";

const ACTIVE = new Set(["queued", "running"]);

function Stats({ d }: { d: Discovery }) {
  const s = (d.stats ?? {}) as Record<string, unknown>;
  const routes = (s.routes ?? {}) as { api?: number; pages?: number };
  const persisted = (s.persisted ?? {}) as Record<string, number>;
  const llm = (s.llm ?? {}) as { enabled?: boolean; accepted?: number; rejected?: unknown[]; errors?: string[]; tokens?: number };
  const items: [string, string | number][] = [
    ["Frameworks", ((s.frameworks as string[] | undefined) ?? []).join(", ") || "none detected"],
    ["API routes", routes.api ?? 0],
    ["Pages", routes.pages ?? 0],
    ["OpenAPI operations", (s.operations as number | undefined) ?? 0],
    ["Candidates", (s.candidates as number | undefined) ?? 0],
    ["New / updated", `${persisted.created ?? 0} / ${persisted.updated ?? 0}`],
    ["Files excluded", (s.excluded_files as number | undefined) ?? 0],
    ["Model", llm.enabled ? `${llm.accepted ?? 0} added, ${llm.rejected?.length ?? 0} rejected, ${llm.tokens ?? 0} tokens` : "off (deterministic only)"],
  ];
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
      {items.map(([k, v]) => (
        <div key={k}><dt className="text-[var(--muted)]">{k}</dt><dd className="font-medium">{v}</dd></div>
      ))}
      {llm.errors?.length ? <p className="col-span-full text-xs text-[var(--muted)]">Model step degraded: {llm.errors.join("; ")}. The deterministic baseline was kept.</p> : null}
    </dl>
  );
}

function Progress({ d }: { d: Discovery }) {
  return (
    <ol className="space-y-1 text-sm">
      {d.progress.map((p, i) => {
        const { step, at, ...rest } = p as { step: string; at: string } & Record<string, unknown>;
        return (
          <li key={i} className="flex gap-2">
            <span className="w-40 font-mono text-xs">{step}</span>
            <span className="text-xs text-[var(--muted)]">{Object.entries(rest).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") || "–" : String(v)}`).join(" · ")}</span>
            <span className="ml-auto text-xs text-[var(--muted)]">{fmtDate(at)}</span>
          </li>
        );
      })}
    </ol>
  );
}

export default function DiscoveryPage() {
  const { id, role } = useProject();
  const qc = useQueryClient();
  const engineer = hasRole(role, "engineer");
  const repo = useQuery({ queryKey: ["repo", id], queryFn: () => apiFetch<LinkedRepo | null>(`/v1/projects/${id}/repository`) });
  const specs = useQuery({ queryKey: ["openapi", id], queryFn: () => apiFetch<OpenApiSpec[]>(`/v1/projects/${id}/openapi-specs`) });
  const runs = useQuery({
    queryKey: ["discoveries", id],
    queryFn: () => apiFetch<Discovery[]>(`/v1/projects/${id}/discoveries`),
    refetchInterval: (q) => ((q.state.data ?? []).some((d) => ACTIVE.has(d.status)) ? 2000 : false),
  });
  const [ref, setRef] = useState("");
  const [archive, setArchive] = useState<File | null>(null);
  const [chosenSpecs, setChosenSpecs] = useState<Set<string>>(new Set());
  const [specText, setSpecText] = useState("");
  const targets = useQuery({ queryKey: ["targets", id], queryFn: () => apiFetch<Target[]>(`/v1/projects/${id}/targets`), enabled: engineer });
  const secrets = useQuery({ queryKey: ["secrets", id], queryFn: () => apiFetch<SecretMeta[]>(`/v1/projects/${id}/secrets`), enabled: engineer });
  const verified = (targets.data ?? []).filter((t) => t.status === "verified");
  const [explore, setExplore] = useState({ target: "", budget: "150", minutes: "15", user: "", password: "" });

  const start = useMutation({
    mutationFn: async () => {
      let archiveId: string | undefined;
      if (archive) {
        const up = await apiFetch<SourceArchive>(`/v1/projects/${id}/source-archives`, {
          method: "POST", body: archive, headers: { "Content-Type": "application/octet-stream" },
        });
        archiveId = up.archive_artifact_id;
      }
      return apiFetch<Discovery>(`/v1/projects/${id}/discoveries`, {
        method: "POST", idempotencyKey: newIdempotencyKey(),
        body: JSON.stringify({
          commit_ref: ref || null,
          archive_artifact_id: archiveId ?? null,
          openapi_spec_ids: [...chosenSpecs],
          browser_exploration: explore.target
            ? {
                target_id: explore.target,
                action_budget: Number(explore.budget),
                time_budget_s: Number(explore.minutes) * 60,
                username_secret: explore.user || null,
                password_secret: explore.password || null,
              }
            : null,
        }),
      });
    },
    onSuccess: () => { setArchive(null); qc.invalidateQueries({ queryKey: ["discoveries", id] }); },
  });
  const register = useMutation({
    mutationFn: () => apiFetch<OpenApiSpec>(`/v1/projects/${id}/openapi-specs`, { method: "POST", body: JSON.stringify({ document: specText }) }),
    onSuccess: (s) => { setSpecText(""); setChosenSpecs((c) => new Set(c).add(s.id)); qc.invalidateQueries({ queryKey: ["openapi", id] }); },
  });
  const cancel = useMutation({
    mutationFn: (did: string) => apiFetch(`/v1/discoveries/${did}:cancel`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["discoveries", id] }),
  });
  const latest = runs.data?.[0];

  return (
    <div className="space-y-6">
      {engineer && (
        <Card title="Discover workflows">
          <p className="mb-4 text-sm text-[var(--muted)]">
            Discovery reads your code at a pinned commit (Next.js, Express and FastAPI routes), imports OpenAPI documents, and proposes
            candidate workflows with the evidence they came from. Nothing is tested until you approve it. Files with credentials,
            <code> .env</code> files and binaries are excluded before analysis. Discovery finds what the code and specs expose; it does not
            guarantee every feature is covered.
          </p>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-3">
              {repo.data ? (
                <Field label={`Commit or branch of ${repo.data.full_name}`} hint={`Default: ${repo.data.default_branch}`}>
                  <Input value={ref} onChange={(e) => setRef(e.target.value)} placeholder={repo.data.default_branch} />
                </Field>
              ) : (
                <Field label="Source archive (.zip or .tar.gz)" hint={<>No repository connected. <Link className="underline" href={`/projects/${id}/repository`}>Connect GitHub</Link> or upload the source tree.</>}>
                  <Input type="file" accept=".zip,.tar.gz,.tgz" onChange={(e) => setArchive(e.target.files?.[0] ?? null)} />
                </Field>
              )}
              <fieldset>
                <legend className="text-sm font-medium">OpenAPI documents</legend>
                <StateView query={specs} empty={<p className="text-sm text-[var(--muted)]">None registered.</p>}>
                  {(list) => (
                    <ul className="mt-1 space-y-1 text-sm">
                      {list.map((s) => (
                        <li key={s.id}>
                          <label className="flex items-center gap-2">
                            <input type="checkbox" checked={chosenSpecs.has(s.id)} onChange={() => setChosenSpecs((c) => { const n = new Set(c); if (n.has(s.id)) n.delete(s.id); else n.add(s.id); return n; })} />
                            {s.operation_count} operations · {s.source} · {fmtDate(s.created_at)}
                          </label>
                        </li>
                      ))}
                    </ul>
                  )}
                </StateView>
              </fieldset>
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">Browser exploration (optional)</legend>
                <p className="text-xs text-[var(--muted)]">
                  A runner crawls a verified target in a real browser, staying on that origin, within the budget below. Deletes, payments,
                  account changes, email sends and logout are never clicked; they are listed as skipped. Forms it submits become
                  candidates for you to review.
                </p>
                <Select aria-label="Target to explore" value={explore.target} onChange={(e) => setExplore({ ...explore, target: e.target.value })}>
                  <option value="">Don&apos;t explore</option>
                  {verified.map((t) => <option key={t.id} value={t.id}>{t.base_url} ({t.environment})</option>)}
                </Select>
                {explore.target && (
                  <div className="grid grid-cols-2 gap-2">
                    <Field label="Action budget"><Input type="number" min={1} max={500} value={explore.budget} onChange={(e) => setExplore({ ...explore, budget: e.target.value })} /></Field>
                    <Field label="Time budget (minutes)"><Input type="number" min={1} max={30} value={explore.minutes} onChange={(e) => setExplore({ ...explore, minutes: e.target.value })} /></Field>
                    <Field label="Test user secret">
                      <Select value={explore.user} onChange={(e) => setExplore({ ...explore, user: e.target.value })}>
                        <option value="">No login</option>
                        {(secrets.data ?? []).map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
                      </Select>
                    </Field>
                    <Field label="Test password secret">
                      <Select value={explore.password} onChange={(e) => setExplore({ ...explore, password: e.target.value })}>
                        <option value="">No login</option>
                        {(secrets.data ?? []).map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
                      </Select>
                    </Field>
                  </div>
                )}
                {targets.isSuccess && verified.length === 0 && (
                  <p className="text-xs text-[var(--muted)]"><Link className="underline" href={`/projects/${id}/targets`}>Verify a target</Link> to enable exploration.</p>
                )}
              </fieldset>
              <ErrorNote error={start.error} />
              <Button onClick={() => start.mutate()} disabled={start.isPending || (latest !== undefined && ACTIVE.has(latest.status))}>
                {start.isPending ? "Starting…" : "Start discovery"}
              </Button>
            </div>
            <div className="space-y-2">
              <label className="block text-sm font-medium" htmlFor="openapi">Register an OpenAPI 3.x document (JSON or YAML)</label>
              <textarea id="openapi" className="h-40 w-full rounded-md border border-[var(--border)] bg-transparent p-2 font-mono text-xs" value={specText} onChange={(e) => setSpecText(e.target.value)} spellCheck={false} />
              <ErrorNote error={register.error} />
              <Button variant="secondary" onClick={() => register.mutate()} disabled={specText.length < 10 || register.isPending}>Register document</Button>
            </div>
          </div>
        </Card>
      )}

      {latest && (
        <Card title={<span className="flex items-center gap-2">Latest discovery <Badge>{latest.status}</Badge></span>}
          actions={engineer && ACTIVE.has(latest.status) && <Button variant="ghost" onClick={() => cancel.mutate(latest.id)}>Cancel</Button>}>
          <div className="space-y-4">
            {latest.stats && <Stats d={latest} />}
            {latest.error && <p className="text-sm text-[var(--danger)]">{String((latest.error as { message?: string }).message ?? "failed")}</p>}
            <Progress d={latest} />
            {latest.status === "awaiting_review" && (
              <p className="text-sm">Candidates are ready. <Link className="underline" href={`/projects/${id}/workflows?status=candidate`}>Review workflows</Link></p>
            )}
          </div>
        </Card>
      )}

      <Card title="History">
        <StateView query={runs} empty={<p>No discoveries yet.</p>}>
          {(list) => (
            <Table head={["Started", "Status", "Commit", "Candidates"]}>
              {list.map((d) => (
                <tr key={d.id}>
                  <td className="px-2 py-2">{fmtDate(d.created_at)}</td>
                  <td className="px-2 py-2"><Badge>{d.status}</Badge></td>
                  <td className="px-2 py-2 font-mono text-xs">{/^0+$/.test(d.commit_sha) ? "archive" : d.commit_sha.slice(0, 10)}</td>
                  <td className="px-2 py-2">{String((d.stats as { candidates?: number } | null)?.candidates ?? "–")}</td>
                </tr>
              ))}
            </Table>
          )}
        </StateView>
      </Card>
    </div>
  );
}
