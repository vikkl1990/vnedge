import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet, ApiError } from "../api";
import { TerminalBadge, TerminalPanel } from "./Terminal";

export interface QueueRow {
  row_key: string; lane: string; decision_id: string | null; snapshot_id: string | null;
  strategy_id: string | null; symbol: string | null; timeframe: string | null;
  entry_clock: string | null; mode: string | null; side: string | null;
  decision_open: string | null; decision_close: string | null;
  observed_at: string | null; last_event_at: string | null;
  stage: string; population: string; has_fill: boolean; evidence_status: string;
  primary_reason: string | null; failed_gates: string[];
  decision_context?: { explanation: string } | null;
  entry: number | null; stop: number | null; target: number | null;
  cost_profile_id: string | null; ml_probability: number | null; ml_status: string;
  outcome_basis: string; research_net_usd: number | null; booked_net_usd: number | null;
}
interface Source { lane: string; state: string; caught_up: boolean; invalid_records: number; skipped_bytes?: number; resync_reason?: string }
interface QueuePage {
  generated_at: string; last_event_at: string | null; revision: string;
  rows: QueueRow[]; next_cursor: string | null; sources: Source[];
  active_lanes_truncated: boolean;
  summary: { total: number; decisions: number; with_fills: number; rejected: number; identity_gaps: number };
  facets: Record<string, string[]>;
}
interface QueueDetail {
  row: QueueRow & {
    envelope: Record<string, unknown> | null;
    orders: { client_order_id: string; state: string; filled_quantity: number; intent_recorded: boolean; submitted: boolean }[];
    timeline: { event_id: string; kind: string; observed_at: string | null; reason: string | null;
      failures: string[]; quote_sequence: number | string | null; bbo_ts: string | null; quote_age_ms: number | null }[];
  };
}

export function queueTime(value: string | null): string {
  if (!value || !Number.isFinite(Date.parse(value))) return "—";
  return new Date(value).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
}
export function queuePrice(value: number | null): string {
  return value == null || !Number.isFinite(value) ? "—" : value.toLocaleString("en-US", { maximumFractionDigits: 8 });
}
export function queueOutcome(row: Pick<QueueRow, "outcome_basis" | "research_net_usd" | "has_fill">): string {
  if (row.outcome_basis === "research_observation" && row.research_net_usd != null)
    return `Research sim. ${row.research_net_usd >= 0 ? "+" : "−"}$${Math.abs(row.research_net_usd).toFixed(2)}`;
  return row.has_fill ? "Fill recorded · PnL in Book" : "No recorded fill";
}
export function queueReason(row: Pick<QueueRow, "stage" | "primary_reason" | "decision_context">): string {
  // A past setup explanation must not hide a later risk/order rejection.
  return (row.stage === "evaluated" ? row.decision_context?.explanation : null)
    || row.primary_reason || "No reason recorded";
}

const VIEWS = [
  ["decisions", "Armed decisions"], ["evaluations", "Evaluations"],
  ["orders", "Orders / fills"], ["research", "Research observations"], ["", "All records"],
];
const human = (value: string | null) => value?.replace(/_/g, " ") || "not recorded";

function EvidenceDetail({ rowKey, revision, onClose }: { rowKey: string; revision: string; onClose: () => void }) {
  const query = useQuery({ queryKey: ["signal-queue-detail", rowKey, revision],
    queryFn: () => apiGet<QueueDetail>(`/api/signal-queue/${rowKey}`), retry: false });
  const row = query.data?.row;
  return <section aria-label="Signal evidence inspector" className="mt-5 rounded-xl border border-info/30 bg-inset/70 p-4">
    <header className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">Decision evidence</h3>
      <button className="rounded border border-line px-3 py-1 text-xs" onClick={onClose}>Close inspector</button></header>
    {query.isPending && <p role="status" className="mt-3 text-dim">Loading journal evidence…</p>}
    {query.isError && <p role="alert" className="mt-3 text-warn">Evidence unavailable or outside the recent window. Refresh the queue; no data has been inferred.</p>}
    {row && <>
      <div className="my-4 grid gap-3 text-xs md:grid-cols-2 xl:grid-cols-3">
        {[["Decision ID", row.decision_id], ["Snapshot ID", row.snapshot_id], ["Lane", row.lane],
          ["Decision open", queueTime(row.decision_open)], ["Decision close", queueTime(row.decision_close)],
          ["First record", queueTime(row.observed_at)], ["Entry clock", row.entry_clock],
          ["Cost profile", row.cost_profile_id], ["Outcome basis", row.outcome_basis]].map(([label, value]) =>
            <div key={label}><span className="text-dim">{label}</span><p className="mt-1 break-all font-mono">{value || "not recorded"}</p></div>)}
      </div>
      <p className="text-xs text-warn">ML: no recorded model prediction. No confidence score or probability is invented.</p>
      <p className="my-3 text-xs">All failed checks: {row.failed_gates.length ? row.failed_gates.join(" · ") : "none recorded"}</p>
      {!!row.orders.length && <div className="my-3 space-y-2">{row.orders.map(order => <div key={order.client_order_id} className="rounded border border-line p-2 text-xs font-mono break-all">
        {order.client_order_id} · {human(order.state)} · cumulative filled {queuePrice(order.filled_quantity)}
        {(!order.intent_recorded || !order.submitted) && <span className="text-warn"> · incomplete order chain</span>}
      </div>)}</div>}
      <ol className="mt-4 max-h-80 space-y-3 overflow-auto" aria-label="Journal event timeline">
        {row.timeline.map(event => <li key={event.event_id} className="border-l-2 border-info/30 pl-3 text-xs">
          <div className="flex flex-wrap justify-between gap-2"><strong>{human(event.kind)}</strong><time className="text-dim">{queueTime(event.observed_at)}</time></div>
          <p className="mt-1 break-words text-dim">{event.reason || "No reason recorded"}</p>
          {event.quote_sequence != null && <p className="font-mono text-dim">Quote {event.quote_sequence} · {queueTime(event.bbo_ts)} · recorded age {event.quote_age_ms ?? "—"} ms</p>}
        </li>)}
      </ol>
      <details className="mt-4 text-xs"><summary className="cursor-pointer text-info">Frozen envelope</summary>
        <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded bg-black/20 p-3">{row.envelope ? JSON.stringify(row.envelope, null, 2) : "No validated envelope. Diagnostic row only."}</pre>
      </details>
    </>}
  </section>;
}

export function SignalQueue() {
  const [population, setPopulation] = useState("decisions");
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [cursor, setCursor] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 5_000); return () => window.clearInterval(timer); }, []);
  const params = new URLSearchParams({ limit: "25", population, ...filters, ...(cursor ? { cursor } : {}) });
  const query = useQuery({ queryKey: ["signal-queue", params.toString()],
    queryFn: () => apiGet<QueuePage>(`/api/signal-queue?${params}`),
    refetchInterval: cursor ? false : 5_000, refetchOnWindowFocus: !cursor, retry: false });
  const data = query.data;
  const changed = query.error instanceof ApiError && query.error.status === 409;
  const stale = !!data && !cursor && (!Number.isFinite(Date.parse(data.generated_at)) || now - Date.parse(data.generated_at) > 20_000);
  const sourceProblem = data && (!data.sources.length || data.sources.some(s => s.state !== "ok" || !s.caught_up || s.invalid_records > 0) || data.active_lanes_truncated);
  const refresh = () => { setCursor(""); setSelected(null); void query.refetch(); };
  const changeFilter = (key: string, value: string) => { setFilters(f => ({ ...f, [key]: value })); setCursor(""); setSelected(null); };
  return <TerminalPanel title="Signal queue" meta="JOURNAL EVIDENCE · READ ONLY">
    <div className="mb-5 flex flex-wrap justify-between gap-3">
      <div><p className="text-sm text-dim">From closed-bar evaluation to recorded fill. No synthetic trade activity.</p>
        <p className="mt-2 text-[11px] font-mono text-dim">Recent journal window · not complete history · no pooled PnL</p></div>
      <button className="self-start rounded-md border border-info/40 px-3 py-2 text-xs text-info hover:bg-info/10" onClick={refresh}>Refresh latest</button>
    </div>
    <nav aria-label="Queue populations" className="mb-4 flex flex-wrap gap-2">{VIEWS.map(([id, label]) =>
      <button key={id} aria-pressed={population === id} className={`rounded-md border px-3 py-2 text-xs ${population === id ? "border-info/60 bg-info/10 text-info" : "border-line text-dim hover:text-txt"}`}
        onClick={() => { setPopulation(id); setCursor(""); setSelected(null); }}>{label}</button>)}</nav>
    <div className="mb-4 flex flex-wrap gap-3">{[["strategy_id", "Strategy"], ["symbol", "Market"], ["entry_clock", "Clock"], ["mode", "Mode"]].map(([key, label]) =>
      <label key={key} className="text-[11px] text-dim">{label}<select value={filters[key] || ""} onChange={e => changeFilter(key, e.target.value)} className="ml-2 max-w-64 rounded border border-line bg-inset px-2 py-1.5 text-txt">
        <option value="">All</option>{Array.from(new Set([...(data?.facets[key] ?? []), ...(filters[key] ? [filters[key]] : [])])).map(value => <option key={value} value={value}>{human(value)}</option>)}
      </select></label>)}</div>
    <div className="mb-4 flex flex-wrap items-center gap-3 text-[11px] font-mono text-dim">
      <TerminalBadge tone={query.isError || sourceProblem || stale ? "warn" : "neutral"}>{query.isError ? "READ UNAVAILABLE" : cursor ? "HISTORY PAGE" : query.isPending ? "CONNECTING" : stale ? "INDEX STALE" : sourceProblem ? "PARTIAL SOURCE" : "INDEX CURRENT"}</TerminalBadge>
      <span>{data ? `${data.summary.total} rows · ${data.summary.decisions} decisions · ${data.summary.with_fills} with fills · ${data.summary.identity_gaps} identity gaps` : "Counts unavailable"}</span>
      <span>Last event: {queueTime(data?.last_event_at ?? null)}</span>
    </div>
    {query.isError && <p role="alert" className="mb-4 rounded border border-warn/30 bg-warn/5 p-3 text-xs text-warn">{changed ? "The queue changed while paging. Refresh latest to avoid duplicate or skipped rows." : "Queue read failed. Displayed data may be stale; refresh or wait for reconnection."}</p>}
    {sourceProblem && <p role="status" className="mb-4 text-xs text-warn">Some source records are unavailable, invalid, or still indexing. Missing evidence is not a zero or an approval.</p>}
    {data?.sources.some(s => (s.skipped_bytes ?? 0) > 0) && <p role="status" className="mb-4 text-xs text-dim">Recent tail only: older journal bytes were skipped to catch up. Full journals remain unchanged; incomplete order chains cannot prove fills.</p>}
    <div className="overflow-x-auto rounded-lg border border-line/70">
      <table className="w-full min-w-[1060px] text-left text-xs">
        <thead className="bg-inset/70 text-[10px] uppercase tracking-widest text-dim"><tr>{["Time (decision close / record)", "Market / side", "Setup / clock", "Entry / stop", "Stage", "ML", "Outcome / reason", "Proof"].map(name => <th key={name} className="px-3 py-3">{name}</th>)}</tr></thead>
        <tbody>{data?.rows.map(row => <tr key={row.row_key} className={`border-t border-line/60 transition-colors hover:bg-info/5 ${selected === row.row_key ? "bg-info/10" : ""}`}>
          <td className="whitespace-nowrap px-3 py-4 font-mono text-dim">{queueTime(row.decision_close || row.observed_at)}{!row.decision_close && <small className="block">record time · decision close absent</small>}</td>
          <td className="px-3 py-4"><strong>{row.symbol || "unreported"}</strong><div className="mt-1"><TerminalBadge tone={row.side === "long" ? "good" : row.side === "short" ? "bad" : "neutral"}>{row.side || "no side"}</TerminalBadge></div></td>
          <td className="max-w-64 break-words px-3 py-4"><span>{row.strategy_id || "unreported strategy"}</span><small className="mt-1 block text-dim">{row.timeframe || "—"} · {human(row.entry_clock)} · {human(row.mode)}</small></td>
          <td className="px-3 py-4 font-mono">{queuePrice(row.entry)}<span className="mt-1 block text-dim">SL {queuePrice(row.stop)}</span></td>
          <td className="px-3 py-4"><TerminalBadge tone={row.stage === "identity_gap" ? "bad" : row.stage === "rejected" ? "warn" : row.has_fill ? "info" : "neutral"}>{human(row.stage)}</TerminalBadge></td>
          <td className="px-3 py-4 text-dim" title="No recorded, validated model prediction">—<small className="block">not recorded</small></td>
          <td className="max-w-80 px-3 py-4"><span className={row.outcome_basis === "research_observation" ? "text-warn" : "text-dim"}>{queueOutcome(row)}</span><small className="mt-1 block break-words text-dim">{queueReason(row)}</small><small className="block text-faint">{row.primary_reason}</small></td>
          <td className="px-3 py-4"><button aria-expanded={selected === row.row_key} className="rounded border border-info/30 px-2 py-1 text-info hover:bg-info/10" onClick={() => setSelected(selected === row.row_key ? null : row.row_key)}>Inspect</button><small className="mt-1 block text-dim">{row.evidence_status}</small></td>
        </tr>)}</tbody>
      </table>
      {!data?.rows.length && <div role="status" className="px-5 py-10 text-center text-sm text-dim">{query.isPending ? "Reading recent journal evidence…" : query.isError || sourceProblem ? "Queue evidence unavailable or incomplete. No empty-book conclusion can be drawn." : population === "decisions" ? "No armed decisions in this window. Open Evaluations to see the scanner's recorded denials." : "No matching records in this recent window. Nothing has been inferred."}</div>}
    </div>
    <div className="mt-3 flex justify-between gap-3 text-[11px] text-dim"><span>Counts describe the filtered indexed window, not lifetime totals. Filled ≠ closed profit.</span>
      {data?.next_cursor && <button className="rounded border border-line px-3 py-1 text-info" onClick={() => { setCursor(data.next_cursor!); setSelected(null); }}>Older page →</button>}</div>
    {selected && data && <EvidenceDetail rowKey={selected} revision={data.revision} onClose={() => setSelected(null)} />}
  </TerminalPanel>;
}
