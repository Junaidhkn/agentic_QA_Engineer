"use client";

import { cx } from "@/components/ui";

type Line = { kind: "add" | "del" | "ctx" | "hunk"; text: string; oldNo?: number; newNo?: number };
type FileDiff = { path: string; status: "added" | "deleted" | "modified"; lines: Line[]; additions: number; deletions: number };

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** Parses a unified diff (as produced by the repair pipeline) into files and numbered lines. */
export function parseDiff(diff: string): FileDiff[] {
  const files: FileDiff[] = [];
  let cur: FileDiff | null = null;
  let oldNo = 0;
  let newNo = 0;
  const lines = diff.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("--- ") && lines[i + 1]?.startsWith("+++ ")) {
      const from = line.slice(4).replace(/^a\//, "");
      const to = lines[i + 1].slice(4).replace(/^b\//, "");
      cur = {
        path: to === "/dev/null" ? from : to,
        status: from === "/dev/null" ? "added" : to === "/dev/null" ? "deleted" : "modified",
        lines: [],
        additions: 0,
        deletions: 0,
      };
      files.push(cur);
      i++;
      continue;
    }
    if (!cur) continue;
    const m = HUNK.exec(line);
    if (m) {
      oldNo = Number(m[1]);
      newNo = Number(m[2]);
      cur.lines.push({ kind: "hunk", text: line });
    } else if (line.startsWith("+")) {
      cur.lines.push({ kind: "add", text: line.slice(1), newNo: newNo++ });
      cur.additions++;
    } else if (line.startsWith("-")) {
      cur.lines.push({ kind: "del", text: line.slice(1), oldNo: oldNo++ });
      cur.deletions++;
    } else if (line.startsWith(" ")) {
      cur.lines.push({ kind: "ctx", text: line.slice(1), oldNo: oldNo++, newNo: newNo++ });
    }
  }
  return files;
}

const ROW: Record<Line["kind"], string> = {
  add: "bg-green-50 dark:bg-green-950/40",
  del: "bg-red-50 dark:bg-red-950/40",
  ctx: "",
  hunk: "bg-slate-100 text-[var(--muted)] dark:bg-slate-800",
};
const SIGN: Record<Line["kind"], string> = { add: "+", del: "−", ctx: " ", hunk: "" };

export function DiffViewer({ diff, highlight = [] }: { diff: string; highlight?: string[] }) {
  const files = parseDiff(diff);
  if (!files.length) return <p className="text-sm text-[var(--muted)]">No changes.</p>;
  return (
    <div className="space-y-4">
      {files.map((f) => (
        <section key={f.path} className="overflow-hidden rounded-md border border-[var(--border)]" aria-label={`Changes to ${f.path}`}>
          <header className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] bg-black/5 px-3 py-1.5 text-xs">
            <span className="font-mono">{f.path}</span>
            <span className="text-[var(--muted)]">{f.status}</span>
            {highlight.includes(f.path) && <span className="rounded bg-blue-100 px-1.5 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300">regression test</span>}
            <span className="ml-auto font-mono"><span className="text-green-700 dark:text-green-400">+{f.additions}</span> <span className="text-red-700 dark:text-red-400">−{f.deletions}</span></span>
          </header>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse font-mono text-xs leading-5">
              <tbody>
                {f.lines.map((l, i) => (
                  <tr key={i} className={ROW[l.kind]}>
                    <td className="w-10 select-none pr-2 text-right text-[var(--muted)]">{l.oldNo ?? ""}</td>
                    <td className="w-10 select-none pr-2 text-right text-[var(--muted)]">{l.newNo ?? ""}</td>
                    <td className="w-4 select-none text-center" aria-hidden="true">{SIGN[l.kind]}</td>
                    <td className={cx("whitespace-pre pr-3", l.kind === "hunk" && "py-0.5")}>
                      <span className="sr-only">{l.kind === "add" ? "added: " : l.kind === "del" ? "removed: " : ""}</span>
                      {l.text}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </div>
  );
}
