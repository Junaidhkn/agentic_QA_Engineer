"use client";

import { ClerkProvider, useAuth as useClerkAuth } from "@clerk/nextjs";
import { useEffect, type ReactNode } from "react";

import { useAuth } from "@/lib/auth";

function Bridge() {
  const { getToken, isSignedIn } = useClerkAuth();
  const { registerTokenGetter } = useAuth();
  useEffect(() => {
    if (isSignedIn) registerTokenGetter(() => getToken({ template: "reliability" }));
  }, [getToken, isSignedIn, registerTokenGetter]);
  return null;
}

/** Wraps the app with Clerk when NEXT_PUBLIC_AUTH_MODE=oidc and a publishable key is configured. */
export function MaybeClerk({ children }: { children: ReactNode }) {
  const key = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
  if (process.env.NEXT_PUBLIC_AUTH_MODE !== "oidc" || !key) return <>{children}</>;
  return (
    <ClerkProvider publishableKey={key}>
      <Bridge />
      {children}
    </ClerkProvider>
  );
}
