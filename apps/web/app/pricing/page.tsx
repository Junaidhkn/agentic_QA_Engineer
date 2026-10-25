"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { StateView } from "@/components/ui";
import { publicFetch } from "@/lib/public";
import type { Plan } from "@/lib/types";

const PROFILE_LABELS: Record<string, string> = { smoke: "Smoke", baseline: "Baseline", load: "Load", stress: "Stress", spike: "Spike", soak: "Soak" };

function price(p: Plan): string {
  if (p.price_usd_month === null || p.price_usd_month === undefined) return "Contact us";
  return p.price_usd_month === 0 ? "Free" : `$${p.price_usd_month}/month`;
}

export default function Pricing() {
  const plans = useQuery({ queryKey: ["plans"], queryFn: () => publicFetch<Plan[]>("/v1/plans") });
  const rows: [string, (p: Plan) => string][] = [
    ["Virtual users per run", (p) => p.max_vus.toLocaleString()],
    ["Longest run", (p) => `${Math.round(p.max_duration_s / 60)} min`],
    ["Test types", (p) => p.allowed_profile_types.map((t) => PROFILE_LABELS[t] ?? t).join(", ")],
    ["Virtual-user minutes / month", (p) => p.monthly_vu_minutes.toLocaleString()],
    ["Sandbox minutes / month", (p) => p.monthly_sandbox_minutes.toLocaleString()],
    ["AI tokens / month", (p) => `${(p.monthly_llm_tokens / 1_000_000).toLocaleString()} M`],
    ["Concurrent runs", (p) => String(p.max_concurrent_runs)],
    ["Projects", (p) => String(p.max_projects)],
    ["Artifact retention", (p) => `${p.artifact_retention_days} days`],
    ["Verified repairs as pull requests", (p) => (p.features.repair ? "Yes" : "—")],
    ["Browser exploration", (p) => (p.features.browser_exploration ? "Yes" : "—")],
  ];
  return (
    <main className="mx-auto max-w-5xl px-6 py-16">
      <p className="text-sm"><Link className="underline" href="/">Reliability Engineer</Link></p>
      <h1 className="mt-2 text-3xl font-semibold">Plans</h1>
      <p className="mt-2 text-[var(--muted)]">Limits are enforced before a run starts; nothing is billed beyond your plan. Usage above a limit is refused, not charged.</p>
      <StateView query={plans}>
        {(list) => (
          <div className="mt-8 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr>
                  <th className="px-2 py-2" />
                  {list.map((p) => (
                    <th key={p.id} scope="col" className="px-2 py-2 align-bottom">
                      <div className="text-base font-semibold">{p.name}</div>
                      <div className="font-normal text-[var(--muted)]">{price(p)}</div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map(([label, value]) => (
                  <tr key={label} className="border-t border-[var(--border)]">
                    <th scope="row" className="px-2 py-2 font-normal text-[var(--muted)]">{label}</th>
                    {list.map((p) => <td key={p.id} className="px-2 py-2">{value(p)}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </StateView>
      <p className="mt-8 text-sm text-[var(--muted)]">
        Using the service means accepting the <Link className="underline" href="/legal/terms">Terms</Link> and the{" "}
        <Link className="underline" href="/legal/aup">Acceptable Use Policy</Link>: you only test systems you own or are authorized to test.
      </p>
    </main>
  );
}
