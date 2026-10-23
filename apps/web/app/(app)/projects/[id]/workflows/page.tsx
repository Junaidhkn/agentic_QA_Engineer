"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge, Button, Card, ErrorNote, StateView, Table } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { hasRole } from "@/lib/org";
import { useProject } from "@/lib/project";
import type { IrCheck, Workflow } from "@/lib/types";

const TEMPLATE = {
  ir_version: "1.0",
  key: "checkout.place_order",
  title: "Place an order",
  kind: "api",
  criticality: "critical",
  steps: [
    { id: "cart", request: { method: "POST", path: "/api/cart", json: { productId: 1, qty: 1 } }, extract: { cart_id: "$.id" }, expect: { status: [200, 201] } },
    { id: "order", request: { method: "POST", path: "/api/orders", json: { cartId: "{{cart_id}}" } }, expect: { status: [201] } },
  ],
  load: { weight: 1, think_time_s: [1, 3] },
};

function WorkflowEditor({ projectId, initial, onDone }: { projectId: string; initial?: Workflow; onDone: () => void }) {
  const [text, setText] = useState(JSON.stringify(initial?.spec ?? TEMPLATE, null, 2));
  const [check, setCheck] = useState<IrCheck | null>(null);
  const parse = () => JSON.parse(text) as Record<string, unknown>;
  const validate = useMutation({ mutationFn: () => apiFetch<IrCheck>("/v1/workflow-ir:validate", { method: "POST", body: JSON.stringify({ spec: parse() }) }), onSuccess: setCheck });
  const save = useMutation({
    mutationFn: () => initial
      ? apiFetch<Workflow>(`/v1/workflows/${initial.id}`, { method: "PATCH", body: JSON.stringify({ spec: parse() }) })
      : apiFetch<Workflow>(`/v1/projects/${projectId}/workflows`, { method: "POST", body: JSON.stringify({ spec: parse() }) }),
    onSuccess: onDone,
  });
  return (
    <div className="space-y-2">
      <label className="block text-sm font-medium" htmlFor="ir">Workflow IR (JSON)</label>
      <textarea id="ir" className="h-80 w-full rounded-md border border-[var(--border)] bg-transparent p-2 font-mono text-xs" value={text} onChange={(e) => { setText(e.target.value); setCheck(null); }} spellCheck={false} />
      {check && (check.valid ? <p className="text-sm text-[var(--ok)]">Valid.</p> : <ul className="list-disc pl-5 text-sm text-[var(--danger)]">{check.errors.map((e) => <li key={e}>{e}</li>)}</ul>)}
      <ErrorNote error={validate.error ?? save.error} />
      <div className="flex gap-2">
        <Button variant="secondary" onClick={() => validate.mutate()}>Validate</Button>
        <Button onClick={() => save.mutate()} disabled={save.isPending}>{initial ? "Save changes" : "Create workflow"}</Button>
        <Button variant="ghost" onClick={onDone}>Cancel</Button>
      </div>
    </div>
  );
}

export default function WorkflowsPage() {
  const { id, role } = useProject();
  const qc = useQueryClient();
  const workflows = useQuery({ queryKey: ["workflows", id], queryFn: () => apiFetch<Workflow[]>(`/v1/projects/${id}/workflows`) });
  const [editing, setEditing] = useState<Workflow | "new" | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const refresh = () => { setEditing(null); setSelected(new Set()); qc.invalidateQueries({ queryKey: ["workflows", id] }); };
  const review = useMutation({
    mutationFn: (action: "approve" | "reject") => apiFetch(`/v1/projects/${id}/workflows:bulk-review`, { method: "POST", body: JSON.stringify({ [action]: [...selected] }) }),
    onSuccess: refresh,
  });
  const engineer = hasRole(role, "engineer");
  const toggle = (wid: string) => setSelected((s) => { const n = new Set(s); if (n.has(wid)) n.delete(wid); else n.add(wid); return n; });

  return (
    <div className="space-y-4">
      {editing ? (
        <Card title={editing === "new" ? "New workflow" : `Edit ${editing.key}`}><WorkflowEditor projectId={id} initial={editing === "new" ? undefined : editing} onDone={refresh} /></Card>
      ) : (
        <Card title="Workflows" actions={engineer && (
          <div className="flex gap-2">
            {selected.size > 0 && <><Button variant="secondary" onClick={() => review.mutate("approve")}>Approve {selected.size}</Button><Button variant="ghost" onClick={() => review.mutate("reject")}>Reject</Button></>}
            <Button onClick={() => setEditing("new")}>Add workflow</Button>
          </div>
        )}>
          <p className="mb-3 text-sm text-[var(--muted)]">Only approved workflows are tested. Candidates come from discovery; review what each one does before approving. Destructive workflows need an admin.</p>
          <ErrorNote error={review.error} />
          <StateView query={workflows} empty={<p>No workflows yet. Run discovery or add one manually.</p>}>
            {(list) => (
              <Table head={["", "Workflow", "Kind", "Criticality", "Source", "Status", ""]}>
                {list.map((w) => (
                  <tr key={w.id}>
                    <td className="px-2 py-2">{engineer && w.status === "candidate" && <input type="checkbox" aria-label={`Select ${w.key}`} checked={selected.has(w.id)} onChange={() => toggle(w.id)} />}</td>
                    <td className="px-2 py-2"><p className="font-medium">{w.title}</p><p className="font-mono text-xs text-[var(--muted)]">{w.key}{w.destructive && " · destructive"}</p></td>
                    <td className="px-2 py-2">{w.kind}</td>
                    <td className="px-2 py-2"><Badge>{w.criticality}</Badge></td>
                    <td className="px-2 py-2">{w.source}{w.discovery_confidence !== null && ` (${Math.round(Number(w.discovery_confidence) * 100)}%)`}</td>
                    <td className="px-2 py-2"><Badge>{w.status}</Badge></td>
                    <td className="px-2 py-2 text-right">{engineer && <Button variant="ghost" onClick={() => setEditing(w)}>Edit</Button>}</td>
                  </tr>
                ))}
              </Table>
            )}
          </StateView>
        </Card>
      )}
    </div>
  );
}
