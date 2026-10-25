"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { StateView } from "@/components/ui";
import { publicFetch } from "@/lib/public";
import type { LegalDocument } from "@/lib/types";

export default function LegalIndex() {
  const docs = useQuery({ queryKey: ["legal-docs"], queryFn: () => publicFetch<LegalDocument[]>("/v1/legal/documents") });
  return (
    <main className="mx-auto max-w-3xl px-6 py-16">
      <p className="text-sm"><Link className="underline" href="/">Reliability Engineer</Link></p>
      <h1 className="mt-2 text-3xl font-semibold">Legal</h1>
      <StateView query={docs}>
        {(list) => (
          <ul className="mt-6 space-y-2">
            {list.map((d) => (
              <li key={d.document}>
                <Link className="underline" href={`/legal/${d.document}`}>{d.title}</Link>
                <span className="ml-2 text-sm text-[var(--muted)]">version {d.version}</span>
              </li>
            ))}
          </ul>
        )}
      </StateView>
    </main>
  );
}
