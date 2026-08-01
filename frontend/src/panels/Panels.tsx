// Core cockpit panels, wired to the live endpoints via TanStack Query. This is
// the proof-of-parity subset (header, snapshot, journal); the remaining classic
// panels port onto these same primitives incrementally.

import { DenseTable, TerminalBadge, TerminalPanel, type Column } from "../components/Terminal";
import { useJournal, useSnapshot, useWhoAmI } from "../queries";
import type { JournalRow } from "../api";

const usd = (n: unknown) =>
  typeof n === "number" ? `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}` : "—";

export function Header() {
  const who = useWhoAmI();
  const snap = useSnapshot();
  const mode = (snap.data?.mode as string) ?? "…";
  const role = who.data?.role ?? "…";
  return (
    <header className="flex items-center justify-between gap-4 flex-wrap">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-md border border-brand/40 grid place-items-center text-brand font-mono">
          VN
        </div>
        <div>
          <div className="text-[15px] font-semibold">VN Edge — Control Room</div>
          <div className="text-[11px] font-mono text-dim">React · TanStack · v2</div>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <TerminalBadge tone="info">mode {mode}</TerminalBadge>
        <TerminalBadge tone="neutral">{who.data?.name ?? "…"} · {role}</TerminalBadge>
      </div>
    </header>
  );
}

export function SnapshotPanel() {
  const { data, isLoading, isError } = useSnapshot();
  const equity = data?.equity;
  return (
    <TerminalPanel title="Book" meta={isLoading ? "loading…" : isError ? "error" : "live · 5s"}>
      <div className="flex items-end gap-8">
        <div>
          <div className="text-[11px] uppercase text-dim font-mono">Equity</div>
          <div className="text-3xl font-mono tabular-nums">{usd(equity)}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase text-dim font-mono">Mode</div>
          <div className="text-xl font-mono">{(data?.mode as string) ?? "—"}</div>
        </div>
      </div>
    </TerminalPanel>
  );
}

export function JournalPanel() {
  const { data, isLoading } = useJournal(50);
  const rows = data ?? [];
  const cols: Column<JournalRow>[] = [
    { key: "lane", header: "Lane", render: (r) => <span className="font-mono">{r.lane ?? r.symbol ?? "—"}</span> },
    { key: "side", header: "Side", render: (r) => r.side ?? "—" },
    {
      key: "pnl",
      header: "Net PnL",
      align: "right",
      render: (r) => (
        <span className={typeof r.net_pnl_usd === "number" && r.net_pnl_usd < 0 ? "text-short" : "text-long"}>
          {usd(r.net_pnl_usd)}
        </span>
      ),
    },
    { key: "exit", header: "Exit", render: (r) => <span className="text-dim">{r.exit_reason ?? "—"}</span> },
  ];
  return (
    <TerminalPanel title="Journal" meta={isLoading ? "loading…" : `${rows.length} rows · 20s`}>
      <DenseTable columns={cols} rows={rows} empty="no closed trades yet" />
    </TerminalPanel>
  );
}
