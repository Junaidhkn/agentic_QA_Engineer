"use client";

import { createContext, useCallback, useContext, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";

import { setTokenGetter } from "@/lib/api";

/**
 * Authentication abstraction (ADR-0018).
 *
 * - `dev` mode (local only): the user types an email; requests carry `Bearer dev:<email>`,
 *   which the API accepts only when RE_ENV is local/test.
 * - `oidc` mode: a managed IdP (Clerk by default, see docs/architecture/vendors.md) supplies
 *   short-lived JWTs; wired in components/auth/ClerkBridge when the publishable key is set.
 */
export type AuthMode = "dev" | "oidc";
export const AUTH_MODE: AuthMode = (process.env.NEXT_PUBLIC_AUTH_MODE as AuthMode) ?? "dev";
const DEV_KEY = "re.dev.email";

interface AuthState {
  mode: AuthMode;
  email: string | null;
  signedIn: boolean;
  signInDev: (email: string) => void;
  signOut: () => void;
  registerTokenGetter: (getter: () => Promise<string | null>) => void;
}

const AuthContext = createContext<AuthState | null>(null);

function readDevEmail(): string | null {
  try {
    return window.localStorage.getItem(DEV_KEY);
  } catch {
    return null;
  }
}

const AUTH_EVENT = "re-auth-change";

function subscribe(callback: () => void): () => void {
  window.addEventListener("storage", callback);
  window.addEventListener(AUTH_EVENT, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(AUTH_EVENT, callback);
  };
}

if (typeof window !== "undefined" && AUTH_MODE === "dev") {
  setTokenGetter(async () => {
    const current = readDevEmail();
    return current ? `dev:${current}` : null;
  });
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const email = useSyncExternalStore(subscribe, readDevEmail, () => null);
  const [oidcSignedIn, setOidcSignedIn] = useState(false);

  const signInDev = useCallback((value: string) => {
    try {
      window.localStorage.setItem(DEV_KEY, value);
    } catch {
      /* storage unavailable */
    }
    window.dispatchEvent(new Event(AUTH_EVENT));
  }, []);

  const signOut = useCallback(() => {
    try {
      window.localStorage.removeItem(DEV_KEY);
    } catch {
      /* ignore */
    }
    window.dispatchEvent(new Event(AUTH_EVENT));
    setOidcSignedIn(false);
    setTokenGetter(async () => null);
  }, []);

  const registerTokenGetter = useCallback((getter: () => Promise<string | null>) => {
    setTokenGetter(getter);
    setOidcSignedIn(true);
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      mode: AUTH_MODE,
      email,
      signedIn: AUTH_MODE === "dev" ? Boolean(email) : oidcSignedIn,
      signInDev,
      signOut,
      registerTokenGetter,
    }),
    [email, oidcSignedIn, signInDev, signOut, registerTokenGetter],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
