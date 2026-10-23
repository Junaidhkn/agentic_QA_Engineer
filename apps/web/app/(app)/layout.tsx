"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { cx } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { useOrg } from "@/lib/org";

const NAV = [
  { href: "/projects", label: "Projects" },
  { href: "/approvals", label: "Approvals" },
  { href: "/settings/members", label: "Settings" },
];

export default function AppLayout({ children }: { children: ReactNode }) {
  const { signedIn, email, signOut, mode } = useAuth();
  const { orgs, current, select } = useOrg();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (mode === "dev" && !signedIn) router.replace("/login");
  }, [mode, signedIn, router]);

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="border-b border-[var(--border)] bg-[var(--surface)] p-4 md:w-56 md:border-b-0 md:border-r">
        <Link href="/projects" className="block text-lg font-semibold">Reliability Engineer</Link>
        {orgs.length > 0 && (
          <label className="mt-4 block text-xs text-[var(--muted)]">
            Organization
            <select
              className="mt-1 w-full rounded border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-sm text-[var(--text)]"
              value={current?.id ?? ""}
              onChange={(e) => select(e.target.value)}
            >
              {orgs.map((o) => <option key={o.id} value={o.id}>{o.name} ({o.role})</option>)}
            </select>
          </label>
        )}
        <nav className="mt-4 flex gap-1 md:flex-col" aria-label="Main">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={cx("rounded px-2 py-1.5 text-sm", pathname.startsWith(item.href.split("/").slice(0, 2).join("/")) ? "bg-black/5 font-medium" : "hover:bg-black/5")}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="mt-6 text-xs text-[var(--muted)]">
          {email && <p className="truncate">{email}</p>}
          <button className="mt-1 underline" onClick={() => { signOut(); router.push("/login"); }}>Sign out</button>
        </div>
        {current?.suspended && <p className="mt-3 rounded bg-red-100 p-2 text-xs text-red-800">This organization is suspended.</p>}
      </aside>
      <main className="flex-1 p-6">{children}</main>
    </div>
  );
}
