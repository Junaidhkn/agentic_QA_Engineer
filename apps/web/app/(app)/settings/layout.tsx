"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { cx, PageHeader } from "@/components/ui";

const TABS = [["members", "Members"], ["api-keys", "API keys"], ["integrations", "Integrations"], ["usage", "Usage"], ["audit", "Audit log"]];

export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return (
    <div>
      <PageHeader title="Organization settings" />
      <nav aria-label="Settings" className="mb-6 flex flex-wrap gap-1 border-b border-[var(--border)]">
        {TABS.map(([slug, label]) => (
          <Link key={slug} href={`/settings/${slug}`} className={cx("-mb-px border-b-2 px-3 py-2 text-sm", pathname.endsWith(slug) ? "border-[var(--accent)] font-medium" : "border-transparent text-[var(--muted)]")}>{label}</Link>
        ))}
      </nav>
      {children}
    </div>
  );
}
