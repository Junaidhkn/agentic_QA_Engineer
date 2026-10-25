import Link from "next/link";
import type { ReactNode } from "react";

/**
 * Renders our own legal documents (headings, paragraphs, lists, block quotes, tables, **bold**,
 * `code`, links). Builds React nodes — no raw HTML — and maps `x.md` links to `/legal/x`.
 */
function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const k = `${key}-${i++}`;
    if (tok.startsWith("**")) out.push(<strong key={k}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("`")) out.push(<code key={k} className="rounded bg-black/5 px-1">{tok.slice(1, -1)}</code>);
    else {
      const [, label, href] = /\[([^\]]+)\]\(([^)]+)\)/.exec(tok) ?? [];
      const local = /^([a-z-]+)\.md$/.exec(href ?? "");
      out.push(local ? <Link key={k} className="underline" href={`/legal/${local[1]}`}>{label}</Link> : <a key={k} className="underline" href={href} rel="noopener noreferrer">{label}</a>);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ source }: { source: string }) {
  const lines = source.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `b${i}`;
    if (!line.trim()) { i++; continue; }
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      const cls = level === 1 ? "text-2xl font-semibold" : level === 2 ? "mt-6 text-lg font-semibold" : "mt-4 font-semibold";
      blocks.push(level === 1 ? <h1 key={key} className={cls}>{inline(heading[2], key)}</h1> : level === 2 ? <h2 key={key} className={cls}>{inline(heading[2], key)}</h2> : <h3 key={key} className={cls}>{inline(heading[2], key)}</h3>);
      i++;
      continue;
    }
    if (line.startsWith(">")) {
      const quote: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) quote.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push(<blockquote key={key} className="my-3 border-l-4 border-[var(--border)] pl-3 text-sm text-[var(--muted)]">{inline(quote.join(" "), key)}</blockquote>);
      continue;
    }
    if (line.startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        const cells = lines[i++].split("|").slice(1, -1).map((c) => c.trim());
        if (!cells.every((c) => /^-+$/.test(c))) rows.push(cells);
      }
      const [head, ...body] = rows;
      if (!head) continue; // a lone "|" line: nothing to show
      blocks.push(
        <div key={key} className="my-3 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead><tr>{head.map((c, j) => <th key={j} className="border-b border-[var(--border)] px-2 py-1">{inline(c, `${key}h${j}`)}</th>)}</tr></thead>
            <tbody>{body.map((r, ri) => <tr key={ri}>{r.map((c, j) => <td key={j} className="border-b border-[var(--border)] px-2 py-1 align-top">{inline(c, `${key}r${ri}c${j}`)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
      continue;
    }
    const bullet = /^(\s*)(-|\d+\.)\s+/;
    if (bullet.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: string[] = [];
      while (i < lines.length && (bullet.test(lines[i]) || (lines[i].startsWith("   ") && lines[i].trim()))) {
        if (bullet.test(lines[i])) items.push(lines[i].replace(bullet, ""));
        else items[items.length - 1] += ` ${lines[i].trim()}`;
        i++;
      }
      const list = items.map((t, j) => <li key={j}>{inline(t, `${key}i${j}`)}</li>);
      blocks.push(ordered ? <ol key={key} className="my-2 list-decimal space-y-1 pl-6">{list}</ol> : <ul key={key} className="my-2 list-disc space-y-1 pl-6">{list}</ul>);
      continue;
    }
    const para: string[] = [lines[i++]]; // always consume one line, so odd input cannot stall
    while (i < lines.length && lines[i].trim() && !/^(#|>|\||\s*-\s|\s*\d+\.\s)/.test(lines[i])) para.push(lines[i++]);
    blocks.push(<p key={key} className="my-2">{inline(para.join(" "), key)}</p>);
  }
  return <article className="max-w-3xl leading-relaxed">{blocks}</article>;
}
