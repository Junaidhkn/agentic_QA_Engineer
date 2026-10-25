"use client";

import { useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";

import { Button, Card, ErrorNote } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Org } from "@/lib/types";

export default function AcceptInvitation() {
  const { token } = useParams<{ token: string }>();
  const { signedIn } = useAuth();
  const accept = useMutation({ mutationFn: () => apiFetch<Org>(`/v1/invitations/${token}:accept`, { method: "POST" }) });
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <Card title="Join organization" className="w-full max-w-md">
        {!signedIn ? (
          <p className="text-sm">Please <Link className="text-[var(--accent)]" href="/login">sign in</Link> with the invited email first.</p>
        ) : accept.isSuccess ? (
          <p className="text-sm">You joined <strong>{accept.data.name}</strong> as {accept.data.role}. <Link className="text-[var(--accent)]" href="/projects">Go to projects</Link></p>
        ) : (
          <div className="space-y-3">
            <ErrorNote error={accept.error} />
            <Button onClick={() => accept.mutate()} disabled={accept.isPending}>Accept invitation</Button>
          </div>
        )}
      </Card>
    </main>
  );
}
