"use client";

import Link from "next/link";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

import { ApiError } from "@/lib/api";

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

type Variant = "primary" | "secondary" | "danger" | "ghost";

export function Button({
  variant = "primary",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  const styles: Record<Variant, string> = {
    primary: "bg-[var(--accent)] text-white hover:opacity-90",
    secondary: "border border-[var(--border)] bg-[var(--surface)] hover:bg-black/5",
    danger: "bg-[var(--danger)] text-white hover:opacity-90",
    ghost: "hover:bg-black/5",
  };
  return (
    <button
      className={cx(
        "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50",
        styles[variant],
        className,
      )}
      {...props}
    />
  );
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cx("rounded-lg border border-[var(--border)] bg-[var(--surface)] p-4", className)}>
      {(title || actions) && (
        <header className="mb-3 flex items-center justify-between gap-2">
          {title && <h2 className="text-base font-semibold">{title}</h2>}
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-[var(--muted)]">{hint}</span>}
    </label>
  );
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={cx("w-full rounded-md border border-[var(--border)] bg-transparent px-2.5 py-1.5", props.className)}
    />
  );
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={cx("w-full rounded-md border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1.5", props.className)}
    />
  );
}

const BADGE_TONES: Record<string, string> = {
  ok: "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
  warn: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  danger: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
  info: "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300",
  muted: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

const STATUS_TONE: Record<string, keyof typeof BADGE_TONES> = {
  verified: "ok", passed: "ok", valid: "ok", approved: "ok", completed: "ok", resolved: "ok", improved: "ok", ready: "ok",
  pending: "warn", queued: "info", running: "info", provisioning: "info", analyzing: "info", pending_approval: "warn",
  failed: "danger", errored: "danger", invalid: "warn", rejected: "danger", revoked: "danger", expired: "warn",
  cancelled: "muted", candidate: "info", critical: "danger", high: "danger", medium: "warn", low: "muted",
  open: "warn", flaky: "warn", awaiting_review: "info", succeeded: "ok", building: "info", planning: "info", budget_exhausted: "warn", generating: "info", validating: "info", partially_ready: "warn", regressed: "danger", inconclusive: "warn", no_change: "muted",
  coding: "info", verifying: "info", awaiting_approval: "warn", verification_failed: "danger", pr_opened: "ok", abandoned: "muted", skipped: "muted",
};

export function Badge({ children, tone }: { children: ReactNode; tone?: keyof typeof BADGE_TONES }) {
  const key = tone ?? STATUS_TONE[String(children)] ?? "muted";
  return <span className={cx("inline-block rounded px-1.5 py-0.5 text-xs font-medium", BADGE_TONES[key])}>{String(children).replaceAll("_", " ")}</span>;
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof ApiError ? `${error.message} (${error.code})` : String(error);
  return (
    <p role="alert" className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
      {message}
      {error instanceof ApiError && error.requestId && <span className="block text-xs opacity-70">Request {error.requestId}</span>}
    </p>
  );
}

/** Every data view renders loading, error, empty and success states. */
export function StateView<T>({
  query,
  empty,
  children,
}: {
  query: { isLoading: boolean; error: unknown; data: T | undefined };
  empty?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (query.isLoading) return <p className="text-sm text-[var(--muted)]" aria-busy="true">Loading…</p>;
  if (query.error) return <ErrorNote error={query.error} />;
  if (query.data === undefined) return null;
  if (Array.isArray(query.data) && query.data.length === 0 && empty) return <div className="text-sm text-[var(--muted)]">{empty}</div>;
  return <>{children(query.data)}</>;
}

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-[var(--border)] text-xs uppercase text-[var(--muted)]">
          <tr>{head.map((h) => <th key={h} className="px-2 py-2 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-[var(--border)]">{children}</tbody>
      </table>
    </div>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-[var(--muted)]">{description}</p>}
      </div>
      {actions}
    </div>
  );
}

export function TextLink({ href, children }: { href: string; children: ReactNode }) {
  return <Link href={href} className="text-[var(--accent)] hover:underline">{children}</Link>;
}

export function fmtDate(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}
