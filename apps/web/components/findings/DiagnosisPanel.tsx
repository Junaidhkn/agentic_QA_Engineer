"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { Badge, Button, Card, ErrorNote, fmtDate } from "@/components/ui";
import { apiFetch, newIdempotencyKey } from "@/lib/api";
import type { Diagnosis, Hypothesis, Repair } from "@/lib/types";

const ACTIVE = new Set(["queued", "collecting", "hypothesizing", "checking", "experimenting"]);
const PHASE: Record<string, string> = {
  queued: "Queued",
  collecting: "Collecting evidence",
  hypothesizing: "Forming hypotheses",
  checking: "Checking citations",
  experimenting: "Running a confirming experiment in the sandbox",
};
const BAND_TONE = { high: "ok", medium: "warn", low: "muted" } as const;
const CHECK_TONE: Record<string, "ok" | "warn" | "danger"> = { verified: "ok", unverifiable: "warn", failed: "danger" };

type Experiment = { name?: string; result?: string; summary?: string } | null;

/** Diagnosis for one finding: start/re-run, live phase, ranked hypotheses with their verified citations. */
export function DiagnosisPanel({ findingId, canRun, projectId }: { findingId: string; canRun: boolean; projectId: string }) {
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["diagnoses", findingId],
    queryFn: () => apiFetch<Diagnosis[]>(`/v1/findings/${findingId}/diagnoses`),
    refetchInterval: (q) => ((q.state.data ?? []).some((d) => ACTIVE.has(d.status)) ? 3000 : false),
  });
  const latestId = list.data?.[0]?.id;
  const active = list.data?.[0] ? ACTIVE.has(list.data[0].status) : false;
  const detail = useQuery({
    queryKey: ["diagnosis", latestId, list.data?.[0]?.status],
    queryFn: async () => {
      const out = await apiFetch<Diagnosis>(`/v1/diagnoses/${latestId}`);
      // Diagnosis tools attach evidence to the finding; refresh it so citation links resolve.
      void qc.invalidateQueries({ queryKey: ["finding", findingId] });
      return out;
    },
    enabled: Boolean(latestId) && !active,
  });
  const start = useMutation({
    mutationFn: () => apiFetch<Diagnosis>(`/v1/findings/${findingId}/diagnoses`, { method: "POST", idempotencyKey: newIdempotencyKey() }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["diagnoses", findingId] });
      void qc.invalidateQueries({ queryKey: ["finding", findingId] });
    },
  });
  const onFeedback = () => {
    void qc.invalidateQueries({ queryKey: ["diagnosis", latestId] });
  };

  const d = detail.data;
  const latest = list.data?.[0];
  return (
    <Card
      title="Diagnosis"
      actions={canRun && <Button variant={latest ? "secondary" : "primary"} disabled={active || start.isPending} onClick={() => start.mutate()}>{latest ? "Re-run diagnosis" : "Diagnose"}</Button>}
    >
      <ErrorNote error={start.error ?? list.error ?? detail.error} />
      {!latest && !list.isLoading && (
        <p className="text-sm text-[var(--muted)]">No diagnosis yet. The agent reads the run&apos;s evidence and your code, proposes root causes, and keeps only those it can back with verified citations.</p>
      )}
      {latest && active && (
        <p className="flex items-center gap-2 text-sm" role="status" aria-live="polite">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-[var(--accent)]" />
          {PHASE[latest.status] ?? latest.status}… <span className="text-[var(--muted)]">started {fmtDate(latest.created_at)}</span>
        </p>
      )}
      {d && !active && (
        <div className="space-y-4">
          <p className="flex flex-wrap items-center gap-2 text-xs text-[var(--muted)]">
            <Badge>{d.status.replaceAll("_", " ")}</Badge>
            <span>finished {fmtDate(d.finished_at)}</span>
            {d.model_info && <span>· {String(d.model_info.citations_verified ?? 0)}/{String(d.model_info.citations_proposed ?? 0)} citations verified · {String(d.model_info.tool_calls ?? 0)} tool calls</span>}
          </p>
          {(d.hypotheses ?? []).length === 0 && <p className="text-sm">No hypothesis could be backed by verified evidence. See the next steps below.</p>}
          {(d.hypotheses ?? []).map((h) => <HypothesisCard key={h.id} h={h} canRun={canRun} onFeedback={onFeedback} projectId={projectId} />)}
          {d.next_steps.length > 0 && (
            <div>
              <h4 className="text-sm font-medium">Suggested next steps</h4>
              <ul className="ml-5 list-disc text-sm">{d.next_steps.map((s) => <li key={s}>{s}</li>)}</ul>
            </div>
          )}
          <p className="border-t border-[var(--border)] pt-2 text-xs text-[var(--muted)]">{d.disclaimer}</p>
        </div>
      )}
    </Card>
  );
}

function HypothesisCard({ h, canRun, onFeedback, projectId }: { h: Hypothesis; canRun: boolean; onFeedback: () => void; projectId: string }) {
  const f = h.confidence_factors;
  const router = useRouter();
  const repair = useMutation({
    mutationFn: () => apiFetch<Repair>(`/v1/hypotheses/${h.id}/repairs`, { method: "POST", idempotencyKey: newIdempotencyKey(), body: JSON.stringify({}) }),
    onSuccess: (r) => router.push(`/projects/${projectId}/repairs/${r.id}`),
  });
  const exp = (f.experiment ?? null) as Experiment;
  const feedback = useMutation({
    mutationFn: (verdict: string) => apiFetch(`/v1/hypotheses/${h.id}/feedback`, { method: "POST", body: JSON.stringify({ verdict }) }),
    onSuccess: onFeedback,
  });
  return (
    <article className="rounded-md border border-[var(--border)] p-3">
      <header className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-[var(--muted)]">#{h.rank}</span>
        <Badge tone={BAND_TONE[h.confidence]}>{h.confidence} confidence</Badge>
        <Badge tone="info">{h.category.replaceAll("_", " ")}</Badge>
        {h.user_verdict && <Badge>you: {h.user_verdict}</Badge>}
      </header>
      <p className="mt-2 text-sm font-medium">{h.statement}</p>
      <p className="mt-1 text-sm">{h.mechanism}</p>
      {h.fix_direction && <p className="mt-1 text-sm"><span className="text-[var(--muted)]">Fix direction: </span>{h.fix_direction}</p>}
      {h.code_locations.length > 0 && (
        <ul className="mt-2 space-y-0.5 font-mono text-xs">
          {h.code_locations.map((c) => <li key={`${String(c.path)}:${String(c.line ?? "")}`}>{String(c.path)}{c.line ? `:${String(c.line)}` : ""}</li>)}
        </ul>
      )}
      <details className="mt-2 text-sm">
        <summary className="cursor-pointer text-[var(--muted)]">Why this confidence ({h.evidence.filter((e) => e.check === "verified").length} verified citations)</summary>
        <dl className="mt-1 grid gap-x-6 gap-y-0.5 text-xs sm:grid-cols-2">
          <div><dt className="inline text-[var(--muted)]">score: </dt><dd className="inline">{String(f.score ?? "—")} ({String(f.rubric_version ?? "")})</dd></div>
          <div><dt className="inline text-[var(--muted)]">evidence kinds: </dt><dd className="inline">{((f.evidence_kinds as string[] | undefined) ?? []).join(", ") || "—"}</dd></div>
          <div><dt className="inline text-[var(--muted)]">hot-path code: </dt><dd className="inline">{f.hot_path_code ? "yes" : "no"}</dd></div>
          <div><dt className="inline text-[var(--muted)]">contradicting evidence: </dt><dd className="inline">{String(f.contradicting ?? 0)}</dd></div>
          <div><dt className="inline text-[var(--muted)]">failed citations: </dt><dd className="inline">{String(f.failed_citations ?? 0)}</dd></div>
          {Boolean(f.cap) && <div><dt className="inline text-[var(--muted)]">capped at: </dt><dd className="inline">{String(f.cap)}</dd></div>}
          {exp && <div className="sm:col-span-2"><dt className="inline text-[var(--muted)]">experiment: </dt><dd className="inline">{exp.name?.replaceAll("_", " ")} → {exp.result}{exp.summary ? ` (${exp.summary})` : ""}</dd></div>}
        </dl>
        <ul className="mt-2 space-y-1">
          {h.evidence.map((e) => (
            <li key={`${e.id}-${e.relation}`} className="text-xs">
              <Badge tone={CHECK_TONE[e.check] ?? "muted"}>{e.check}</Badge>{" "}
              <span className="text-[var(--muted)]">{e.relation}</span>{" "}
              <a className="text-[var(--accent)] underline" href={`#evidence-${e.id}`}>{e.kind.replaceAll("_", " ")}</a>: {e.claim}
            </li>
          ))}
        </ul>
      </details>
      {canRun && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="text-xs text-[var(--muted)]">Was this the cause?</span>
          {(["confirmed", "refuted", "unsure"] as const).map((v) => (
            <Button key={v} variant="ghost" className="text-xs" aria-pressed={h.user_verdict === v} disabled={feedback.isPending} onClick={() => feedback.mutate(v)}>
              {v === "confirmed" ? "Yes" : v === "refuted" ? "No" : "Not sure"}
            </Button>
          ))}
          <ErrorNote error={feedback.error} />
          <Button variant="secondary" className="ml-auto text-xs" disabled={repair.isPending} onClick={() => repair.mutate()}
            title="An agent writes a regression test and a fix; an independent pipeline verifies it before you review it">
            Propose a fix
          </Button>
          <ErrorNote error={repair.error} />
        </div>
      )}
    </article>
  );
}
