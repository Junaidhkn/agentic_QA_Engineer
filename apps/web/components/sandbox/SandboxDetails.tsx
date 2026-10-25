"use client";

import { Fragment } from "react";

import { Badge, Table } from "@/components/ui";

type Dict = Record<string, unknown>;

/** The build plan: how the app is built and started, and what runs around it. */
export function PlanView({ plan }: { plan: Dict }) {
  const services = Object.keys((plan.services as Dict | undefined) ?? {});
  const cmd = (c: unknown) => (Array.isArray(c) ? (c as string[]).join(" ") : "—");
  const rows: [string, string][] = [
    ["Strategy", `${String(plan.strategy)}${plan.framework ? ` · ${String(plan.framework)}` : ""}`],
    ["Install", ((plan.install as unknown[]) ?? []).map(cmd).join("; ") || "—"],
    ["Build", ((plan.build as unknown[]) ?? []).map(cmd).join("; ") || "—"],
    ["Start", cmd(plan.start)],
    ["Health check", `GET ${String(plan.health_path)}`],
    ["Services", services.join(", ") || "none"],
    ["Migrations", ((plan.migrate as unknown[]) ?? []).map(cmd).join("; ") || "none"],
    ["Seed", ((plan.seed as unknown[]) ?? []).map(cmd).join("; ") || "none"],
    ["Stubbed integrations", ((plan.stubs as Dict[]) ?? []).map((s) => `${String(s.env)} → stub`).join(", ") || "none"],
  ];
  return (
    <div className="space-y-2 text-sm">
      <dl className="grid grid-cols-[10rem_1fr] gap-x-3 gap-y-1">
        {rows.map(([k, v]) => (<Fragment key={k}><dt className="text-[var(--muted)]">{k}</dt><dd className="font-mono text-xs break-all">{v}</dd></Fragment>))}
      </dl>
      {((plan.placeholders as string[]) ?? []).length > 0 && (
        <p className="text-xs">Required settings filled with placeholders: <code>{(plan.placeholders as string[]).join(", ")}</code>. Store real values as project secrets named <code>SANDBOX_&lt;NAME&gt;</code>.</p>
      )}
      {((plan.warnings as string[]) ?? []).map((w) => <p key={w} className="text-xs text-[var(--muted)]">⚠ {w}</p>)}
      {plan.unsupported ? <p className="text-sm text-[var(--danger)]">Cannot build in a sandbox: {String(plan.unsupported)}. Use a preview URL target instead.</p> : null}
    </div>
  );
}

/** What happened when the sandbox ran: phases, health, data steps and database evidence. */
export function ReportView({ report }: { report: Dict }) {
  const phases = Object.entries((report.phases as Record<string, number>) ?? {});
  const db = (report.db as Dict | null) ?? null;
  const tables = ((db?.tables as Dict[]) ?? []).slice(0, 6);
  const statements = ((db?.statements as Dict[]) ?? []).slice(0, 5);
  const conns = (db?.connections as Dict | undefined) ?? undefined;
  const samples = (report.db_samples as Dict[]) ?? [];
  const peak = samples.reduce((m, s) => Math.max(m, Number(s.active ?? 0) + Number(s.idle_in_transaction ?? 0)), 0);
  return (
    <div className="space-y-4 text-sm">
      <p className="flex flex-wrap gap-2">
        <Badge tone={report.healthy ? "ok" : "danger"}>{report.healthy ? "healthy" : "not healthy"}</Badge>
        <Badge tone="muted">{`driver ${String(report.driver)}`}</Badge>
        <Badge tone="muted">{`migrations ${String(report.migrations)}`}</Badge>
        <Badge tone="muted">{`seed ${String(report.seed)}`}</Badge>
        <Badge tone="muted">{`telemetry ${String(report.instrumentation)}`}</Badge>
      </p>
      {report.error ? <p className="text-[var(--danger)]">Failed during {String(report.error_phase)}: {String(report.error)}</p> : null}
      {phases.length > 0 && (
        <Table head={["Phase", "Seconds"]}>
          {phases.map(([k, v]) => <tr key={k}><td className="px-2 py-1">{k}</td><td className="px-2 py-1 tabular-nums">{v.toFixed(1)}</td></tr>)}
        </Table>
      )}
      {tables.length > 0 && (
        <div>
          <p className="mb-1 font-medium">Database tables (by rows read sequentially)</p>
          <Table head={["Table", "Seq scans", "Rows read by seq scans", "Index scans", "Live rows"]}>
            {tables.map((t) => (
              <tr key={String(t.relname)}>
                <td className="px-2 py-1 font-mono text-xs">{String(t.relname)}</td>
                <td className="px-2 py-1 tabular-nums">{String(t.seq_scan)}</td>
                <td className="px-2 py-1 tabular-nums">{String(t.seq_tup_read)}</td>
                <td className="px-2 py-1 tabular-nums">{String(t.idx_scan ?? 0)}</td>
                <td className="px-2 py-1 tabular-nums">{String(t.n_live_tup)}</td>
              </tr>
            ))}
          </Table>
        </div>
      )}
      {conns && <p className="text-xs">Connections at the end: {String(conns.total)} of {String(conns.max)} max; peak sampled during the run: {peak}.</p>}
      {statements.length > 0 && (
        <div>
          <p className="mb-1 font-medium">Slowest statements (total time)</p>
          <Table head={["Query", "Calls", "Mean ms", "Total ms"]}>
            {statements.map((q, i) => (
              <tr key={i}><td className="px-2 py-1 font-mono text-xs break-all">{String(q.query)}</td><td className="px-2 py-1">{String(q.calls)}</td><td className="px-2 py-1">{String(q.mean_ms)}</td><td className="px-2 py-1">{String(q.total_ms)}</td></tr>
            ))}
          </Table>
        </div>
      )}
      {((report.stub_calls as Dict[]) ?? []).length > 0 && (
        <p className="text-xs">Stubbed calls: {((report.stub_calls as Dict[]) ?? []).map((c) => `${String(c.call)} ×${String(c.count)}`).join(", ")}</p>
      )}
    </div>
  );
}
