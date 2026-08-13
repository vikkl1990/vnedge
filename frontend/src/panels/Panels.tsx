// Core cockpit panels, wired to the live endpoints via TanStack Query. This is
// the growing parity subset (header, book, risk, positions, journal); the
// remaining classic panels port onto these same primitives incrementally.

import { DenseTable, TerminalBadge, TerminalPanel, type Column } from "../components/Terminal";
import { useJournal, useSnapshot, useWhoAmI } from "../queries";
import type { JournalRow, LaneRow, PlanOverlay, Position, RegimeReading, Snapshot, TrialScorecard } from "../api";

const usd = (n: unknown) =>
  typeof n === "number" ? `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}` : "—";
const signed = (n: unknown) => (typeof n === "number" && n < 0 ? "text-short" : "text-long");

const ageMs = (ms: unknown) => {
  const n = Number(ms);
  if (!Number.isFinite(n)) return "—";
  return n < 1000 ? `${Math.round(n)} ms` : n < 60000 ? `${(n / 1000).toFixed(1)} s` : `${Math.round(n / 60000)} m`;
};

export function Header() {
  const who = useWhoAmI();
  const snap = useSnapshot();
  const mode = (snap.data?.mode as string) ?? "…";
  const role = who.data?.role ?? "…";
  const age = snap.data?.snapshot_age_ms;
  const ageTone = typeof age === "number" ? (age > 10000 ? "bad" : age > 3000 ? "warn" : "good") : "neutral";
  return (
    <header className="flex items-center justify-between gap-4 flex-wrap">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-md border border-brand/40 grid place-items-center text-brand font-mono">
          VN
        </div>
        <div>
          <div className="flex items-center gap-2">
            <span className="text-[15px] font-semibold">VN Edge — Control Room</span>
            <TerminalBadge tone="warn">partial — full ops at /</TerminalBadge>
          </div>
          <div className="text-[11px] font-mono text-dim">React · TanStack · v2</div>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <TerminalBadge tone={ageTone as never}>age {ageMs(age)}</TerminalBadge>
        <TerminalBadge tone="info">mode {mode}</TerminalBadge>
        <TerminalBadge tone={snap.data?.kill_switch_active ? "bad" : "good"}>
          kill {snap.data?.kill_switch_active ? "ARMED" : "clear"}
        </TerminalBadge>
        <TerminalBadge tone="neutral">
          {who.data?.name ?? "…"} · {role}
        </TerminalBadge>
      </div>
    </header>
  );
}

// ---- health helpers (mirror the classic strip; UNKNOWN never fakes OK) -------
type Band = "ok" | "degraded" | "blocked" | "unknown";
const TM_AGE_SOFT: Record<string, number> = { "1m": 1500, "5m": 3000, "15m": 5000, "1h": 8000, "4h": 15000 };
const BAND_TONE: Record<Band, string> = { ok: "good", degraded: "warn", blocked: "bad", unknown: "neutral" };
const RANK: Record<Band, number> = { blocked: 3, degraded: 2, ok: 1, unknown: 0 };
const worse = (a: Band, b: Band): Band => (RANK[a] >= RANK[b] ? a : b);
const skipCount = (o?: Record<string, number> | null) =>
  o ? Object.values(o).reduce((a, b) => a + (Number(b) || 0), 0) : 0;

function laneRows(s?: Snapshot): LaneRow[] {
  if (!s) return [];
  if (Array.isArray(s.lanes) && s.lanes.length) return s.lanes;
  if (s.time_machine) {
    const sess = (s.session ?? {}) as Record<string, unknown>;
    return [
      {
        strategy_id: s.strategy_id as string | undefined,
        symbol: s.symbol,
        timeframe: (sess.timeframe as string) ?? Object.keys(s.time_machine.health ?? {})[0] ?? "1h",
        mode: s.mode,
        cost_profile: (s.cost_profile as string) ?? (sess.cost_profile as string),
        feed: s.feed_health?.candles,
        time_machine: s.time_machine,
        latency: s.latency ?? null,
        decision_skips: s.decision_skips ?? ((sess.decision_skips as Record<string, number>) ?? null),
        regime: s.regime ?? ((sess.regime as RegimeReading) ?? null),
        plan_overlay: s.plan_overlay ?? ((sess.plan_overlay as PlanOverlay) ?? null),
        equity: s.equity,
        peak_equity: s.peak_equity as number | undefined,
        drawdown_pct: (sess.drawdown_pct as number) ?? null,
        dd_limit_pct: (sess.dd_limit_pct as number) ?? null,
        trial_scorecard: (sess.trial_scorecard as TrialScorecard) ?? null,
        bands: (s.lane_bands as LaneRow["bands"]) ?? null,
      },
    ];
  }
  return [];
}

function computeChips(s?: Snapshot): Record<string, { band: Band; label: string }> {
  const lanes = laneRows(s);
  const kill = !!s?.kill_switch_active;
  // CANDLE
  let candle: Band = "unknown";
  let cLabel = "no telemetry";
  for (const l of lanes) {
    const tm = l.time_machine;
    if (!tm?.health) continue;
    if (candle === "unknown") { candle = "ok"; cLabel = "ok"; }
    const h = tm.health[l.timeframe ?? ""];
    if (h && h !== "ok") { candle = worse(candle, "blocked"); cLabel = `decision-TF ${h}`; }
    if (skipCount(l.decision_skips) > 0) { candle = worse(candle, "blocked"); cLabel = "arms blocked"; }
    const a1 = tm.age_ms?.["1m"];
    if (a1 != null && a1 > TM_AGE_SOFT["1m"] && candle === "ok") { candle = "degraded"; cLabel = "1m age soft"; }
  }
  // DECISION
  let decision: Band = "unknown";
  let dLabel = "no telemetry";
  let skips = false, haveLat = false, latSoft = false;
  for (const l of lanes) {
    if (skipCount(l.decision_skips) > 0) skips = true;
    const p95 = l.latency?.decision_lag_ms?.p95;
    if (typeof p95 === "number") { haveLat = true; if (p95 > 50) latSoft = true; }
  }
  if (skips) { decision = "blocked"; dLabel = "new arms blocked"; }
  else if (haveLat) { decision = latSoft ? "degraded" : "ok"; dLabel = latSoft ? "compute lag" : "ok"; }
  // FEED
  let feed: Band = "unknown";
  let fLabel = "—";
  const cand = String(s?.feed_health?.candles ?? "").toLowerCase();
  if (cand) {
    if (cand.includes("ok") || cand.includes("live")) { feed = "ok"; fLabel = "live"; }
    else if (cand.includes("warm")) { feed = "degraded"; fLabel = "warming"; }
    else { feed = "blocked"; fLabel = cand.slice(0, 12); }
  }
  // RISK
  let risk: Band = "ok";
  let rLabel = "ok";
  const rs = String(s?.risk_status ?? "ok").toLowerCase();
  const streak = Number(s?.consecutive_losses) || 0;
  if (kill) { risk = "blocked"; rLabel = "kill tripped"; }
  else if (rs && rs !== "ok") { risk = "blocked"; rLabel = rs.slice(0, 14); }
  else if (streak >= 3) { risk = "degraded"; rLabel = `${streak} loss streak`; }
  // SYSTEM
  let system: Band = "ok";
  let sLabel = "nominal";
  if (kill) { system = "blocked"; sLabel = "kill tripped"; }
  else {
    for (const x of [candle, decision, feed, risk]) if (x !== "unknown") system = worse(system, x);
    sLabel = system === "ok" ? "nominal" : system === "degraded" ? "degraded" : "blocked";
  }
  return { SYSTEM: { band: system, label: sLabel }, FEED: { band: feed, label: fLabel }, CANDLE: { band: candle, label: cLabel }, DECISION: { band: decision, label: dLabel }, RISK: { band: risk, label: rLabel } };
}

const BAND_BORDER: Record<Band, string> = {
  ok: "border-l-long",
  degraded: "border-l-warn",
  blocked: "border-l-short",
  unknown: "border-l-line",
};

export function StatusStrip() {
  const { data } = useSnapshot();
  // prefer server-computed chips (health_bands.py) — one source for both cockpits;
  // fall back to the client computation only if the snapshot predates them.
  const server = data?.chips as Record<string, { band: Band; label: string }> | undefined;
  const chips = server && Object.keys(server).length ? server : computeChips(data);
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-2.5">
      {Object.entries(chips).map(([name, c]) => (
        <div
          key={name}
          className={`flex items-center gap-2 rounded-md border border-line border-l-[3px] ${BAND_BORDER[c.band]} bg-inset px-3 py-2.5`}
        >
          <span className="text-[10px] font-mono font-extrabold tracking-wider text-dim">{name}</span>
          <span className="ml-auto">
            <TerminalBadge tone={BAND_TONE[c.band] as never}>{c.label}</TerminalBadge>
          </span>
        </div>
      ))}
    </div>
  );
}

export function LanesPanel() {
  const { data } = useSnapshot();
  const lanes = laneRows(data);
  const cols: Column<LaneRow>[] = [
    { key: "strategy_id", header: "Lane", render: (r) => (r.strategy_id ?? "").replace(/_v\d+$/, "") },
    { key: "mode", header: "Mode", render: (r) => String(r.mode ?? "—").split(" ")[0] },
    { key: "cost_profile", header: "Cost", render: (r) => r.cost_profile ?? "—" },
    {
      key: "regime",
      header: "Regime",
      render: (r) => (r.regime?.label ? `${r.regime.label}${r.regime.confidence != null ? ` ${Math.round(r.regime.confidence * 100)}%` : ""}` : "—"),
    },
    {
      key: "health",
      header: "Candle",
      render: (r) => {
        const h = r.time_machine?.health?.[r.timeframe ?? ""];
        const tone = !h ? "neutral" : h === "ok" ? "good" : "bad";
        return <TerminalBadge tone={tone as never}>{h ?? "n/a"}</TerminalBadge>;
      },
    },
    {
      key: "dd",
      header: "DD / limit",
      render: (r) => {
        const dd = r.drawdown_pct;
        const lim = r.dd_limit_pct;
        if (dd == null) return "—";
        // prefer the server band (health_bands.py); else classify client-side
        const srv = r.bands?.dd;
        const tone = srv
          ? { ok: "good", degraded: "warn", blocked: "bad", unknown: "neutral" }[srv] ?? "neutral"
          : lim == null ? "neutral" : dd >= lim ? "bad" : dd >= 0.8 * lim ? "warn" : "good";
        return <TerminalBadge tone={tone as never}>{`${dd.toFixed(2)}%${lim != null ? ` / ${lim}%` : ""}`}</TerminalBadge>;
      },
    },
    {
      key: "trial",
      header: "Trial",
      render: (r) => {
        const v = r.trial_scorecard?.verdict;
        if (!v) return "—";
        const tone = v === "PASS" ? "good" : v === "FAIL" ? "bad" : "warn";
        return <TerminalBadge tone={tone as never}>{v}</TerminalBadge>;
      },
    },
    {
      key: "plan",
      header: "Plan",
      render: (r) =>
        r.plan_overlay?.side
          ? `${r.plan_overlay.side} ${r.plan_overlay.expected_net_bps ?? "?"}bps ${r.plan_overlay.gate_ok ? "PASS" : "REJECT"}`
          : "—",
    },
  ];
  return (
    <TerminalPanel title="Lanes" meta={`${lanes.length} · mode · cost · regime · candle · plan (observe-only)`}>
      {lanes.length ? <DenseTable columns={cols} rows={lanes} /> : <div className="text-faint text-[12px] p-2">No lane telemetry.</div>}
    </TerminalPanel>
  );
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase text-dim font-mono">{label}</div>
      <div className={`text-2xl font-mono tabular-nums ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

export function BookPanel() {
  const { data, isLoading, isError } = useSnapshot();
  return (
    <TerminalPanel title="Book" meta={isLoading ? "loading…" : isError ? "error" : "live · 5s"}>
      <div className="flex items-end gap-10 flex-wrap">
        <Kpi label="Equity" value={usd(data?.equity)} />
        <Kpi label="Realized" value={usd(data?.realized_pnl)} tone={signed(data?.realized_pnl)} />
        <Kpi label="Unrealized" value={usd(data?.unrealized_pnl)} tone={signed(data?.unrealized_pnl)} />
        <Kpi label="Peak" value={usd(data?.peak_equity)} />
      </div>
    </TerminalPanel>
  );
}

export function RiskPanel() {
  const { data } = useSnapshot();
  const status = (data?.risk_status as string) ?? "—";
  const statusTone = status.toLowerCase().includes("ok") ? "good" : status === "—" ? "neutral" : "warn";
  return (
    <TerminalPanel title="Risk" meta="gateway · breaker · kill">
      <div className="flex items-center gap-3 flex-wrap">
        <TerminalBadge tone={statusTone as never}>{status}</TerminalBadge>
        <TerminalBadge tone={data?.live_trading_enabled ? "bad" : "good"}>
          live {data?.live_trading_enabled ? "ENABLED" : "locked"}
        </TerminalBadge>
      </div>
      <div className="flex items-end gap-10 flex-wrap mt-4">
        <Kpi label="Daily PnL" value={usd(data?.daily_pnl)} tone={signed(data?.daily_pnl)} />
        <Kpi
          label="Loss streak"
          value={typeof data?.consecutive_losses === "number" ? String(data.consecutive_losses) : "—"}
        />
        <Kpi label="Fills" value={typeof data?.fills === "number" ? String(data.fills) : "—"} />
        <Kpi label="Fees" value={usd(data?.fees_usd)} />
      </div>
    </TerminalPanel>
  );
}

export function PositionsPanel() {
  const { data } = useSnapshot();
  const rows = (data?.positions as Position[] | undefined) ?? [];
  const cols: Column<Position>[] = [
    { key: "sym", header: "Symbol", render: (r) => <span className="font-mono">{r.symbol ?? "—"}</span> },
    { key: "side", header: "Side", render: (r) => r.side ?? "—" },
    { key: "qty", header: "Qty", align: "right", render: (r) => (typeof r.quantity === "number" ? r.quantity : "—") },
    {
      key: "upnl",
      header: "uPnL",
      align: "right",
      render: (r) => <span className={signed(r.unrealized_pnl_usd)}>{usd(r.unrealized_pnl_usd)}</span>,
    },
  ];
  return (
    <TerminalPanel title="Positions" meta={`${rows.length} open`}>
      <DenseTable columns={cols} rows={rows} empty="flat — no open positions" />
    </TerminalPanel>
  );
}

const num = (n: unknown, d = 2) => (typeof n === "number" ? n.toFixed(d) : "—");

export function MarketPanel() {
  const { data } = useSnapshot();
  const p = data?.price ?? null;
  const fr = data?.funding_rate;
  return (
    <TerminalPanel title="Market" meta={(data?.symbol as string) ?? "—"}>
      {p ? (
        <div className="flex items-end gap-10 flex-wrap">
          <Kpi label="Mid" value={typeof p.mid === "number" ? p.mid.toLocaleString() : "—"} />
          <Kpi label="Bid" value={typeof p.bid === "number" ? p.bid.toLocaleString() : "—"} />
          <Kpi label="Ask" value={typeof p.ask === "number" ? p.ask.toLocaleString() : "—"} />
          <Kpi label="Spread" value={`${num(p.spread_bps, 1)} bps`} />
          <Kpi
            label="Funding"
            value={typeof fr === "number" ? `${(fr * 100).toFixed(4)}%` : "—"}
            tone={typeof fr === "number" ? signed(fr) : ""}
          />
        </div>
      ) : (
        <div className="text-[12px] font-mono text-dim">no live quote (warming / no book)</div>
      )}
    </TerminalPanel>
  );
}

export function FeedPanel() {
  const { data } = useSnapshot();
  const f = data?.feed_health ?? {};
  // Freshness is computed SERVER-side into these status strings (OK / stale) —
  // trust them rather than recomputing against a client clock and a field whose
  // epoch semantics vary by context.
  const tone = (v?: string) =>
    !v ? "neutral" : v.toLowerCase().includes("ok") ? "good" : "warn";
  const chip = (label: string, v?: string) => (
    <div className="flex items-center gap-2">
      <span className="text-[11px] uppercase text-dim font-mono">{label}</span>
      <TerminalBadge tone={tone(v) as never}>{v ?? "—"}</TerminalBadge>
    </div>
  );
  return (
    <TerminalPanel title="Feed health" meta={f.exchange ?? "—"}>
      <div className="flex items-center gap-6 flex-wrap">
        {chip("candles", f.candles)}
        {chip("funding", f.funding)}
        {chip("OI", f.open_interest)}
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
      render: (r) => <span className={signed(r.net_pnl_usd)}>{usd(r.net_pnl_usd)}</span>,
    },
    { key: "exit", header: "Exit", render: (r) => <span className="text-dim">{r.exit_reason ?? "—"}</span> },
  ];
  return (
    <TerminalPanel title="Journal" meta={isLoading ? "loading…" : `${rows.length} rows · 20s`}>
      <DenseTable columns={cols} rows={rows} empty="no closed trades yet" />
    </TerminalPanel>
  );
}
