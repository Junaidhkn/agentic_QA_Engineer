"use client";

import { SignIn } from "@clerk/nextjs";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button, Card, Field, Input } from "@/components/ui";
import { AUTH_MODE, useAuth } from "@/lib/auth";

export default function LoginPage() {
  const { signInDev } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");

  if (AUTH_MODE === "oidc" && process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY) {
    return (
      <main className="flex min-h-screen items-center justify-center p-6">
        <SignIn forceRedirectUrl="/projects" />
      </main>
    );
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    signInDev(email.trim().toLowerCase());
    router.push("/projects");
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <Card title="Sign in (development mode)" className="w-full max-w-sm">
        <form onSubmit={submit} className="space-y-4">
          <Field label="Email" hint="Dev sign-in only works when the API runs with RE_ENV=local or test.">
            <Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
          </Field>
          <Button type="submit" className="w-full justify-center">Continue</Button>
        </form>
      </Card>
    </main>
  );
}
