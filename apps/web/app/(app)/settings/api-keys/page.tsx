"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Badge, Button, Card, ErrorNote, Field, fmtDate, Input, StateView, Table } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole, useOrg } from "@/lib/org";
import type { ApiKey, ApiKeyCreated } from "@/lib/types";

const SCOPES = ["runs:read", "runs:write", "projects:read"];

export default function ApiKeysPage() {
  const { current } = useOrg();
  const orgId = current?.id;
  const qc = useQueryClient();
  const keys = useQuery({ queryKey: ["keys", orgId], queryFn: () => apiFetch<ApiKey[]>(`/v1/orgs/${orgId}/api-keys`), enabled: hasRole(current?.role, "admin") });
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["runs:read", "runs:write"]);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const create = useMutation({
    mutationFn: () => apiFetch<ApiKeyCreated>(`/v1/orgs/${orgId}/api-keys`, { method: "POST", body: JSON.stringify({ name, scopes, expires_in_days: 90 }) }),
    onSuccess: (k) => { setCreated(k); setName(""); qc.invalidateQueries({ queryKey: ["keys", orgId] }); },
  });
  const revoke = useMutation({ mutationFn: (id: string) => apiFetch(`/v1/orgs/${orgId}/api-keys/${id}`, { method: "DELETE" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["keys", orgId] }) });
  if (!hasRole(current?.role, "admin")) return <p className="text-sm">Only admins can manage API keys.</p>;
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <Card title="API keys (for CI)" className="lg:col-span-2">
        {created && (
          <div role="status" className="mb-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
            Copy this key now — it will not be shown again:<code className="mt-1 block break-all">{created.secret}</code>
          </div>
        )}
        <StateView query={keys} empty={<p>No keys.</p>}>
          {(list) => (
            <Table head={["Name", "Prefix", "Scopes", "Expires", "Last used", ""]}>
              {list.map((k) => (
                <tr key={k.id}>
                  <td className="px-2 py-2">{k.name}</td>
                  <td className="px-2 py-2 font-mono text-xs">{k.key_prefix}</td>
                  <td className="px-2 py-2">{k.scopes.join(", ")}</td>
                  <td className="px-2 py-2">{fmtDate(k.expires_at)}</td>
                  <td className="px-2 py-2">{fmtDate(k.last_used_at)}</td>
                  <td className="px-2 py-2 text-right">{k.revoked_at ? <Badge>revoked</Badge> : <Button variant="ghost" onClick={() => revoke.mutate(k.id)}>Revoke</Button>}</td>
                </tr>
              ))}
            </Table>
          )}
        </StateView>
      </Card>
      <Card title="Create key">
        <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); create.mutate(); }}>
          <Field label="Name"><Input required value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <fieldset className="space-y-1 text-sm">
            <legend className="mb-1 font-medium">Scopes</legend>
            {SCOPES.map((s) => (
              <label key={s} className="flex items-center gap-2">
                <input type="checkbox" checked={scopes.includes(s)} onChange={(e) => setScopes(e.target.checked ? [...scopes, s] : scopes.filter((x) => x !== s))} />
                {s}
              </label>
            ))}
          </fieldset>
          <ErrorNote error={create.error} />
          <Button type="submit" disabled={create.isPending || scopes.length === 0}>Create</Button>
        </form>
      </Card>
    </div>
  );
}
