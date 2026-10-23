"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState, type FormEvent } from "react";

import { Button, Card, ErrorNote, Field, Input, PageHeader, StateView } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole, useOrg } from "@/lib/org";
import type { Project } from "@/lib/types";

export default function ProjectsPage() {
  const { current } = useOrg();
  const qc = useQueryClient();
  const projects = useQuery({
    queryKey: ["projects", current?.id],
    queryFn: () => apiFetch<Project[]>("/v1/projects"),
    enabled: Boolean(current),
  });
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => apiFetch<Project>("/v1/projects", { method: "POST", body: JSON.stringify({ name }) }),
    onSuccess: () => {
      setName("");
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });
  const canCreate = hasRole(current?.role, "admin");

  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }

  return (
    <>
      <PageHeader
        title="Projects"
        description="One application under test per project."
        actions={canCreate && <Link href="/onboarding" className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-white">Guided setup</Link>}
      />
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <StateView query={projects} empty={<p>No projects yet. Use guided setup to connect your first app.</p>}>
            {(list) => (
              <ul className="grid gap-3 sm:grid-cols-2">
                {list.map((p) => (
                  <li key={p.id}>
                    <Link href={`/projects/${p.id}`} className="block rounded-lg border border-[var(--border)] bg-[var(--surface)] p-4 hover:border-[var(--accent)]">
                      <p className="font-medium">{p.name}</p>
                      <p className="mt-1 text-xs text-[var(--muted)]">SLO p95 &lt; {p.default_slo.p95_ms} ms · errors &lt; {(p.default_slo.error_rate * 100).toFixed(1)}%</p>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </StateView>
        </div>
        {canCreate && (
          <Card title="New project">
            <form onSubmit={submit} className="space-y-3">
              <Field label="Name"><Input required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} /></Field>
              <ErrorNote error={create.error} />
              <Button type="submit" disabled={create.isPending}>Create</Button>
            </form>
          </Card>
        )}
      </div>
    </>
  );
}
