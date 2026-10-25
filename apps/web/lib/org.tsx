"use client";

import { useQuery } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useMemo, useSyncExternalStore, type ReactNode } from "react";

import { apiFetch, setOrgGetter } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Org } from "@/lib/types";

const KEY = "re.org";
const EVENT = "re-org-change";

function read(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function subscribe(cb: () => void) {
  window.addEventListener(EVENT, cb);
  window.addEventListener("storage", cb);
  return () => {
    window.removeEventListener(EVENT, cb);
    window.removeEventListener("storage", cb);
  };
}

if (typeof window !== "undefined") setOrgGetter(read);

interface OrgState {
  orgs: Org[];
  current: Org | null;
  select: (id: string) => void;
  isLoading: boolean;
}

const OrgContext = createContext<OrgState | null>(null);

export function OrgProvider({ children }: { children: ReactNode }) {
  const { signedIn } = useAuth();
  const stored = useSyncExternalStore(subscribe, read, () => null);
  const orgs = useQuery({ queryKey: ["orgs"], queryFn: () => apiFetch<Org[]>("/v1/orgs"), enabled: signedIn });
  const list = useMemo(() => orgs.data ?? [], [orgs.data]);
  const current = list.find((o) => o.id === stored) ?? list[0] ?? null;
  if (typeof window !== "undefined" && current && current.id !== stored) {
    try {
      window.localStorage.setItem(KEY, current.id);
    } catch {
      /* ignore */
    }
  }
  const select = useCallback((id: string) => {
    try {
      window.localStorage.setItem(KEY, id);
    } catch {
      /* ignore */
    }
    window.dispatchEvent(new Event(EVENT));
  }, []);
  const value = useMemo(() => ({ orgs: list, current, select, isLoading: orgs.isLoading }), [list, current, select, orgs.isLoading]);
  return <OrgContext.Provider value={value}>{children}</OrgContext.Provider>;
}

export function useOrg(): OrgState {
  const ctx = useContext(OrgContext);
  if (!ctx) throw new Error("useOrg must be used inside OrgProvider");
  return ctx;
}

export const ROLE_ORDER: Record<string, number> = { viewer: 0, engineer: 1, admin: 2, owner: 3 };
export function hasRole(role: string | undefined, min: string): boolean {
  return role !== undefined && ROLE_ORDER[role] >= ROLE_ORDER[min];
}
