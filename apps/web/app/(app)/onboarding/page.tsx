"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState, type FormEvent } from "react";

import { Badge, Button, Card, ErrorNote, Field, Input, PageHeader, Select } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import type { Project, Target } from "@/lib/types";

/** UF-1: create project → (optional) link repo → register target → verify ownership. */
export default function Onboarding() {
  const qc = useQueryClient();
  const [project, setProject] = useState<Project | null>(null);
  const [target, setTarget] = useState<Target | null>(null);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [env, setEnv] = useState("staging");

  const createProject = useMutation({
    mutationFn: () => apiFetch<Project>("/v1/projects", { method: "POST", body: JSON.stringify({ name }) }),
    onSuccess: (p) => { setProject(p); qc.invalidateQueries({ queryKey: ["projects"] }); },
  });
  const createTarget = useMutation({
    mutationFn: () => apiFetch<Target>(`/v1/projects/${project!.id}/targets`, { method: "POST", body: JSON.stringify({ base_url: url, environment: env }) }),
    onSuccess: setTarget,
  });
  const verify = useMutation({ mutationFn: () => apiFetch<Target>(`/v1/targets/${target!.id}:verify`, { method: "POST" }), onSuccess: setTarget });
  const installUrl = useQuery({
    queryKey: ["gh-install-url"],
    queryFn: () => apiFetch<{ url: string }>("/v1/integrations/github/install-url", { method: "POST" }),
    enabled: Boolean(project),
    retry: false,
  });
  const step = !project ? 1 : !target ? 2 : 3;
  const v = target?.verification as { dns_txt: { name: string; value: string }; http_file: { url: string; content: string } } | undefined;

  return (
    <div className="max-w-2xl space-y-4">
      <PageHeader title="Set up a project" description={`Step ${step} of 3`} />
      <Card title="1. Project">
        {project ? <p className="text-sm">Created <strong>{project.name}</strong>.</p> : (
          <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); createProject.mutate(); }}>
            <Field label="Project name"><Input required value={name} onChange={(e) => setName(e.target.value)} /></Field>
            <ErrorNote error={createProject.error} />
            <Button type="submit" disabled={createProject.isPending}>Create project</Button>
          </form>
        )}
      </Card>
      {project && (
        <Card title="2. Connect GitHub (optional now, needed for discovery and fixes)">
          {installUrl.data ? (
            <p className="text-sm">Install the GitHub App on the repository, then <Link className="text-[var(--accent)]" href={`/projects/${project.id}/repository`}>link it to this project</Link>. <a className="text-[var(--accent)]" href={installUrl.data.url}>Install GitHub App →</a></p>
          ) : <p className="text-sm text-[var(--muted)]">GitHub App is not configured on this server yet (RE_GITHUB_APP_*). You can link a repository later.</p>}
        </Card>
      )}
      {project && (
        <Card title="3. Staging URL">
          {!target ? (
            <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); createTarget.mutate(); }}>
              <Field label="Base URL"><Input required type="url" placeholder="https://staging.example.com" value={url} onChange={(e) => setUrl(e.target.value)} /></Field>
              <Field label="Environment"><Select value={env} onChange={(e) => setEnv(e.target.value)}>{["staging", "preview", "sandbox", "production"].map((x) => <option key={x}>{x}</option>)}</Select></Field>
              <ErrorNote error={createTarget.error} />
              <Button type="submit" disabled={createTarget.isPending}>Register</Button>
            </form>
          ) : (
            <div className="space-y-3 text-sm">
              <p>{target.host} <Badge>{target.status}</Badge></p>
              {target.status !== "verified" && v && (
                <>
                  <p>Add DNS TXT <code className="break-all">{v.dns_txt.name}</code> = <code className="break-all">{v.dns_txt.value}</code>, or serve <code className="break-all">{v.http_file.url}</code> with that value.</p>
                  <ErrorNote error={verify.error} />
                  <Button onClick={() => verify.mutate()} disabled={verify.isPending}>Verify</Button>
                </>
              )}
              {target.status === "verified" && <p>Verified. <Link className="text-[var(--accent)]" href={`/projects/${project.id}`}>Open the project →</Link></p>}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
