"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { MaybeClerk } from "@/components/auth/ClerkBridge";
import { ApiError } from "@/lib/api";
import { AuthProvider } from "@/lib/auth";
import { OrgProvider } from "@/lib/org";

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
          },
        },
      }),
  );
  return (
    <AuthProvider>
      <MaybeClerk>
        <QueryClientProvider client={client}>
          <OrgProvider>{children}</OrgProvider>
        </QueryClientProvider>
      </MaybeClerk>
    </AuthProvider>
  );
}
