"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Button, Card, ErrorNote, Field, fmtDate, Input, StateView, Table } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { SecretMeta } from "@/lib/types";

export default function SecretsPage() {
  const { id, role } = useProject();
  const qc = useQueryClient();
  const secrets = useQuery({ queryKey: ["secrets", id], queryFn: () => apiFetch<SecretMeta[]>(`/v1/projects/${id}/secrets`), enabled: hasRole(role, "engineer") });
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const save = useMutation({
    mutationFn: () => apiFetch<void>(`/v1/projects/${id}/secrets/${name}`, { method: "PUT", body: JSON.stringify({ value }) }),
    onSuccess: () => { setName(""); setValue(""); qc.invalidateQueries({ queryKey: ["secrets", id] }); },
  });
  const remove = useMutation({
    mutationFn: (n: string) => apiFetch<void>(`/v1/projects/${id}/secrets/${n}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["secrets", id] }),
  });
  const admin = hasRole(role, "admin");
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <Card title="Secrets" className="lg:col-span-2">
        <p className="mb-3 text-sm text-[var(--muted)]">Test credentials and tokens used by generated tests. Values are encrypted, never shown again, and only decrypted inside an isolated test run.</p>
        <StateView query={secrets} empty={<p>No secrets.</p>}>
          {(list) => (
            <Table head={["Name", "Created", "Rotated", ""]}>
              {list.map((s) => (
                <tr key={s.name}>
                  <td className="px-2 py-2 font-mono">{s.name}</td>
                  <td className="px-2 py-2">{fmtDate(s.created_at)}</td>
                  <td className="px-2 py-2">{fmtDate(s.rotated_at)}</td>
                  <td className="px-2 py-2 text-right">{admin && <Button variant="ghost" onClick={() => remove.mutate(s.name)}>Delete</Button>}</td>
                </tr>
              ))}
            </Table>
          )}
        </StateView>
      </Card>
      {admin && (
        <Card title="Add or rotate">
          <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); save.mutate(); }}>
            <Field label="Name" hint="UPPER_SNAKE_CASE, e.g. TEST_USER_PASSWORD"><Input required pattern="[A-Z][A-Z0-9_]{1,63}" value={name} onChange={(e) => setName(e.target.value)} /></Field>
            <Field label="Value"><Input required type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} /></Field>
            <ErrorNote error={save.error} />
            <Button type="submit" disabled={save.isPending}>Save</Button>
          </form>
        </Card>
      )}
    </div>
  );
}
