"use client";

import { useMutation } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { Button, Card, ErrorNote } from "@/components/ui";
import { apiFetch } from "@/lib/api";

function Integrations() {
  const params = useSearchParams();
  const install = useMutation({
    mutationFn: () => apiFetch<{ url: string }>("/v1/integrations/github/install-url", { method: "POST" }),
    onSuccess: ({ url }) => window.location.assign(url),
  });
  return (
    <Card title="GitHub">
      {params.get("github") === "installed" && <p role="status" className="mb-2 text-sm text-[var(--ok)]">GitHub App installed. Link a repository from a project&apos;s Repository tab.</p>}
      {params.get("github") === "error" && <p role="alert" className="mb-2 text-sm text-[var(--danger)]">Installation could not be completed ({params.get("code")}).</p>}
      <p className="mb-3 text-sm text-[var(--muted)]">The GitHub App reads code on the repositories you choose and can open pull requests from <code>reliability/*</code> branches after you approve a fix. It never merges or deploys.</p>
      <ErrorNote error={install.error} />
      <Button onClick={() => install.mutate()} disabled={install.isPending}>Install / configure GitHub App</Button>
    </Card>
  );
}

export default function IntegrationsPage() {
  return <Suspense><Integrations /></Suspense>;
}
