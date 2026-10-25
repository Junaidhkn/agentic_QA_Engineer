"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";

import { Markdown } from "@/components/legal/Markdown";
import { StateView } from "@/components/ui";
import { publicFetch } from "@/lib/public";
import type { LegalDocumentText } from "@/lib/types";

export default function LegalDocumentPage() {
  const { doc } = useParams<{ doc: string }>();
  const text = useQuery({ queryKey: ["legal-doc", doc], queryFn: () => publicFetch<LegalDocumentText>(`/v1/legal/documents/${doc}`) });
  return (
    <main className="mx-auto max-w-3xl px-6 py-16">
      <p className="mb-6 text-sm"><Link className="underline" href="/legal">Legal</Link></p>
      <StateView query={text}>{(d) => <Markdown source={d.markdown} />}</StateView>
    </main>
  );
}
