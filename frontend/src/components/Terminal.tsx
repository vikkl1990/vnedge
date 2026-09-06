// Terminal* component library — the small, composable primitives the roadmap
// called for (TerminalPanel, DenseTable, TerminalBadge, TerminalTabs) so panels
// compose instead of living in one monolithic HTML file.

import type { ReactNode } from "react";

export function TerminalPanel({
  title,
  meta,
  children,
}: {
  title: string;
  meta?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-line/70 bg-panel/65 overflow-hidden shadow-[0_18px_55px_rgba(0,0,0,.2)] backdrop-blur-xl">
      <header className="flex items-center justify-between gap-2 px-4 py-3 border-b border-line/70 bg-inset/30 flex-wrap">
        <h2 className="text-[13px] font-semibold tracking-wide text-txt">{title}</h2>
        {meta ? <span className="text-[11px] font-mono text-dim">{meta}</span> : null}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}

type Tone = "neutral" | "good" | "warn" | "bad" | "info";
const toneClass: Record<Tone, string> = {
  neutral: "text-dim border-line",
  good: "text-long border-long/40",
  warn: "text-warn border-warn/40",
  bad: "text-short border-short/40",
  info: "text-info border-info/40",
};

export function TerminalBadge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-md border bg-black/10 px-2 py-[2px] text-[10px] font-mono font-semibold uppercase tracking-[.04em] ${toneClass[tone]}`}
    >
      {children}
    </span>
  );
}

export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  align?: "left" | "right";
}

export function DenseTable<T>({
  columns,
  rows,
  empty = "no rows",
  rowKey,
}: {
  columns: Column<T>[];
  rows: T[];
  empty?: string;
  rowKey?: (row: T, index: number) => string;
}) {
  if (!rows.length) return <div className="text-[12px] font-mono text-dim py-2">{empty}</div>;
  return (
    <div
      className="overflow-x-auto rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
      role="region"
      aria-label="Scrollable data table"
      tabIndex={0}
    >
      <table className="w-full border-separate border-spacing-0 text-[12px]">
        <thead>
          <tr className="text-faint uppercase text-[10px]">
            {columns.map((c, columnIndex) => (
              <th
                key={c.key}
                className={`sticky top-0 z-10 border-b border-line bg-inset font-mono font-normal py-1.5 px-2 ${columnIndex === 0 ? "left-0 z-20" : ""} ${c.align === "right" ? "text-right" : "text-left"}`}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={rowKey?.(row, i) ?? i} className="border-t border-line/60 hover:bg-white/[0.02]">
              {columns.map((c, columnIndex) => (
                <td
                  key={c.key}
                  className={`py-1.5 px-2 tabular-nums ${columnIndex === 0 ? "sticky left-0 z-[5] bg-panel shadow-[1px_0_0_0_rgba(48,54,61,.9)]" : ""} ${c.align === "right" ? "text-right" : "text-left"}`}
                >
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function TerminalTabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: string; label: string }[];
  active: string;
  onChange: (id: string) => void;
}) {
  return (
    <nav className="flex gap-1 flex-wrap" aria-label="Workbench views">
      {tabs.map((t) => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={`relative rounded-lg px-3.5 py-2 text-[11px] font-mono font-semibold border transition-all duration-200 ${
            active === t.id
              ? "border-brand/40 text-txt bg-brand/10 shadow-[inset_0_-2px_0_rgba(88,166,255,.75),0_0_18px_rgba(88,166,255,.06)]"
              : "border-transparent text-dim hover:text-txt hover:bg-white/[.025]"
          }`}
          aria-current={active === t.id ? "page" : undefined}
        >
          {t.label}
        </button>
      ))}
    </nav>
  );
}
