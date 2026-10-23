"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Badge, Button, Card, ErrorNote, Field, fmtDate, Input, Select, StateView, Table } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole, useOrg } from "@/lib/org";
import type { Member } from "@/lib/types";

export default function MembersPage() {
  const { current } = useOrg();
  const qc = useQueryClient();
  const orgId = current?.id;
  const members = useQuery({ queryKey: ["members", orgId], queryFn: () => apiFetch<Member[]>(`/v1/orgs/${orgId}/members`), enabled: hasRole(current?.role, "admin") });
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("engineer");
  const invite = useMutation({
    mutationFn: () => apiFetch(`/v1/orgs/${orgId}/invitations`, { method: "POST", body: JSON.stringify({ email, role }) }),
    onSuccess: () => setEmail(""),
  });
  const change = useMutation({
    mutationFn: ({ userId, newRole }: { userId: string; newRole: string }) => apiFetch(`/v1/orgs/${orgId}/members/${userId}`, { method: "PATCH", body: JSON.stringify({ role: newRole }) }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["members", orgId] }),
  });
  if (!hasRole(current?.role, "admin")) return <p className="text-sm">Only admins can manage members.</p>;
  const owner = current?.role === "owner";
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <Card title="Members" className="lg:col-span-2">
        <ErrorNote error={change.error} />
        <StateView query={members}>
          {(list) => (
            <Table head={["Email", "Role", "Joined"]}>
              {list.map((m) => (
                <tr key={m.user_id}>
                  <td className="px-2 py-2">{m.email}</td>
                  <td className="px-2 py-2">
                    {owner ? (
                      <Select aria-label={`Role for ${m.email}`} value={m.role} onChange={(e) => change.mutate({ userId: m.user_id, newRole: e.target.value })} className="w-32">
                        {["owner", "admin", "engineer", "viewer"].map((r) => <option key={r}>{r}</option>)}
                      </Select>
                    ) : <Badge tone="info">{m.role}</Badge>}
                  </td>
                  <td className="px-2 py-2">{fmtDate(m.joined_at)}</td>
                </tr>
              ))}
            </Table>
          )}
        </StateView>
      </Card>
      <Card title="Invite">
        <form className="space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); invite.mutate(); }}>
          <Field label="Email"><Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
          <Field label="Role"><Select value={role} onChange={(e) => setRole(e.target.value)}>{(owner ? ["admin", "engineer", "viewer"] : ["engineer", "viewer"]).map((r) => <option key={r}>{r}</option>)}</Select></Field>
          <ErrorNote error={invite.error} />
          {invite.isSuccess && <p className="text-sm text-[var(--ok)]">Invitation sent.</p>}
          <Button type="submit" disabled={invite.isPending}>Send invitation</Button>
        </form>
      </Card>
    </div>
  );
}
