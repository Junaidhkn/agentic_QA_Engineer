"use client";

import { useQuery } from "@tanstack/react-query";

import { Card, StateView } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole, useOrg } from "@/lib/org";
import type { Usage } from "@/lib/types";

const LABELS: Record<string, string> = { vu_minutes: "Virtual-user minutes", sandbox_minutes: "Sandbox minutes", llm_tokens: "AI tokens" };

export default function UsagePage() {
  const { current } = useOrg();
  const usage = useQuery({ queryKey: ["usage", current?.id], queryFn: () => apiFetch<Usage>(`/v1/orgs/${current?.id}/usage`), enabled: hasRole(current?.role, "admin") });
  if (!hasRole(current?.role, "admin")) return <p className="text-sm">Only admins can view usage.</p>;
  return (
    <Card title="Usage this month">
      <StateView query={usage}>
        {(u) => (
          <div className="space-y-4">
            <p className="text-sm">Plan: <strong>{u.plan_id}</strong> · max {String(u.limits.max_vus)} VUs · {String(u.limits.max_concurrent_runs)} concurrent runs</p>
            {Object.entries(u.usage).map(([kind, v]) => {
              const pct = v.limit ? Math.min(100, (v.used / v.limit) * 100) : 0;
              return (
                <div key={kind}>
                  <div className="flex justify-between text-sm"><span>{LABELS[kind] ?? kind}</span><span>{v.used.toLocaleString()} / {v.limit.toLocaleString()}</span></div>
                  <div className="mt-1 h-2 rounded bg-black/10" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label={LABELS[kind] ?? kind}>
                    <div className="h-2 rounded bg-[var(--accent)]" style={{ width: `${pct}%` }} />
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </StateView>
    </Card>
  );
}
