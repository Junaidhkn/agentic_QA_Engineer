"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { Markdown } from "@/components/legal/Markdown";
import { Badge, Button, Card, ErrorNote, fmtDate, StateView } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole, useOrg } from "@/lib/org";
import type { LegalDocumentText, LegalStatus } from "@/lib/types";

export default function LegalSettings() {
  const { current } = useOrg();
  const qc = useQueryClient();
  const admin = hasRole(current?.role, "admin");
  const [open, setOpen] = useState<string | null>(null);
  const [agreed, setAgreed] = useState<Set<string>>(new Set());
  const status = useQuery({
    queryKey: ["legal", current?.id],
    queryFn: () => apiFetch<LegalStatus>(`/v1/orgs/${current?.id}/legal`),
    enabled: Boolean(current),
  });
  const text = useQuery({
    queryKey: ["legal-doc", open],
    queryFn: () => apiFetch<LegalDocumentText>(`/v1/legal/documents/${open}`),
    enabled: open !== null,
  });
  const accept = useMutation({
    mutationFn: ({ document, version }: { document: string; version: string }) =>
      apiFetch<LegalStatus>(`/v1/orgs/${current?.id}/legal-acceptances`, { method: "POST", body: JSON.stringify({ document, version }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["legal"] }),
  });

  return (
    <div className="space-y-6">
      <StateView query={status}>
        {(s) => {
          const current_ = new Set(s.accepted.map((a) => `${a.document}@${a.version}`));
          return (
            <Card title="Terms and policies">
              {s.missing_for_runs.length > 0 && (
                <p className="mb-4 rounded bg-amber-100 p-3 text-sm text-amber-900">
                  Tests cannot run until an owner or admin accepts the current Terms of Service and Acceptable Use Policy for this organization.
                </p>
              )}
              <ul className="divide-y divide-[var(--border)]">
                {s.documents.map((d) => {
                  const done = current_.has(`${d.document}@${d.version}`);
                  const when = s.accepted.find((a) => a.document === d.document && a.version === d.version)?.accepted_at;
                  return (
                    <li key={d.document} className="py-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">{d.title}</span>
                        <span className="text-xs text-[var(--muted)]">version {d.version}</span>
                        {d.required_for_runs && <Badge tone="info">required to run tests</Badge>}
                        {d.acceptable && (done ? <Badge tone="ok">{`accepted ${fmtDate(when)}`}</Badge> : <Badge tone="warn">not accepted</Badge>)}
                        <Button variant="ghost" onClick={() => setOpen(open === d.document ? null : d.document)}>{open === d.document ? "Hide" : "Read"}</Button>
                      </div>
                      {open === d.document && (
                        <div className="mt-3 max-h-96 overflow-y-auto rounded border border-[var(--border)] p-4 text-sm">
                          <StateView query={text}>{(t) => <Markdown source={t.markdown} />}</StateView>
                        </div>
                      )}
                      {d.acceptable && !done && admin && (
                        <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
                          <label className="flex items-center gap-2">
                            <input
                              type="checkbox"
                              checked={agreed.has(d.document)}
                              onChange={() => setAgreed((a) => { const n = new Set(a); if (n.has(d.document)) n.delete(d.document); else n.add(d.document); return n; })}
                            />
                            {d.document === "aup"
                              ? "We only test systems we own or are authorized in writing to test, and accept this policy for the organization."
                              : `I accept the ${d.title} on behalf of ${current?.name ?? "this organization"}.`}
                          </label>
                          <Button onClick={() => accept.mutate({ document: d.document, version: d.version })} disabled={!agreed.has(d.document) || accept.isPending}>
                            Accept
                          </Button>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
              {!admin && s.missing_for_runs.length > 0 && <p className="mt-3 text-sm text-[var(--muted)]">Ask an owner or admin of this organization to accept them.</p>}
              <ErrorNote error={accept.error} />
              <p className="mt-4 text-xs text-[var(--muted)]">
                Public copies: <Link className="underline" href="/legal">legal documents</Link>. Acceptances are recorded with who accepted, when and from which address.
              </p>
            </Card>
          );
        }}
      </StateView>
    </div>
  );
}
