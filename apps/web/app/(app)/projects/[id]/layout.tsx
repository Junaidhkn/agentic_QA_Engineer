"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { cx, ErrorNote } from "@/components/ui";
import { useProject } from "@/lib/project";

const TABS = [
  ["", "Overview"],
  ["/targets", "Targets"],
  ["/repository", "Repository"],
  ["/sandbox", "Sandbox"],
  ["/discovery", "Discovery"],
  ["/workflows", "Workflows"],
  ["/suites", "Test suites"],
  ["/profiles", "Load profiles"],
  ["/runs", "Runs"],
  ["/findings", "Findings"],
  ["/reports", "Reports"],
  ["/secrets", "Secrets"],
] as const;

export default function ProjectLayout({ children }: { children: ReactNode }) {
  const { id, project, query } = useProject();
  const pathname = usePathname();
  const base = `/projects/${id}`;
  if (query.error) return <ErrorNote error={query.error} />;
  return (
    <div>
      <p className="text-xs text-[var(--muted)]"><Link href="/projects">Projects</Link> /</p>
      <h1 className="text-2xl font-semibold">{project?.name ?? "…"}</h1>
      <nav aria-label="Project" className="mt-4 mb-6 flex flex-wrap gap-1 border-b border-[var(--border)]">
        {TABS.map(([suffix, label]) => {
          const href = base + suffix;
          const active = suffix === "" ? pathname === base : pathname.startsWith(href);
          return (
            <Link key={suffix} href={href} className={cx("-mb-px border-b-2 px-3 py-2 text-sm", active ? "border-[var(--accent)] font-medium" : "border-transparent text-[var(--muted)] hover:text-[var(--text)]")}>
              {label}
            </Link>
          );
        })}
      </nav>
      {children}
    </div>
  );
}
