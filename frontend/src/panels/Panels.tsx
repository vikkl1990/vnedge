// Core cockpit panels, wired to the live endpoints via TanStack Query. This is
// the growing parity subset (header, book, risk, positions, journal); the
// remaining classic panels port onto these same primitives incrementally.

import { useEffect, useState } from "react";
import { DenseTable, TerminalBadge, TerminalPanel, type Column } from "../components/Terminal";
import { useJournal, useLanes, useRiskSnapshot, useSnapshot, useWhoAmI } from "../queries";
import type { CorrectionLane, JournalRow, LaneHealth, LaneHealthProblem, LaneRow, PlanOverlay, Position, RegimeReading, Snapshot, TrialScorecard } from "../api";

const usd = (n: unknown) =>
  typeof n === "number" ? `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}` : "—";
const signed = (n: unknown) => (typeof n === "number" && n < 0 ? "text-short" : "text-long");

const ageSec = (s: unknown) => {
  const n = Number(s);
  if (!Number.isFinite(n)) return "—";
  return n < 90 ? `${Math.round(n)}s` : n < 5400 ? `${(n / 60).toFixed(1)}m` : `${(n / 3600).toFixed(1)}h`;
};

export function Header() {
  const who = useWhoAmI();
  const risk = useRiskSnapshot();
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const posture = risk.data;
  const role = who.data?.role ?? "…";
  const feedTone = posture?.feed.status === "healthy" ? "good" : posture?.feed.status === "stale" ? "warn" : posture?.feed.status === "gap" ? "bad" : "neutral";
  const time = clock.toLocaleTimeString("en-GB", { hour12: false, timeZone: "UTC" });
  return (
    <header className="sticky top-0 z-30 -mx-2 rounded-xl border border-line bg-bg/95 px-4 py-3 shadow-xl shadow-black/20 backdrop-blur">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-md border border-brand/40 grid place-items-center text-brand font-mono">VN</div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-[15px] font-semibold">VNEDGE</span>
              <TerminalBadge tone="info">{posture?.runtime_mode ?? "syncing"}</TerminalBadge>
              <span className="hidden sm:inline text-[11px] font-mono text-dim">BTC · ETH</span>
            </div>
            <div className="text-[11px] font-mono text-dim">mode: {posture?.runtime_label ?? "…"}</div>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap justify-end">
          <span className="text-[11px] font-mono text-dim">{time} UTC</span>
          <TerminalBadge tone={posture?.capital.enabled ? "bad" : "neutral"}>capital {posture?.capital.enabled ? "ON" : "OFF"}</TerminalBadge>
          <TerminalBadge tone={posture?.kill.active ? "bad" : "neutral"}>kill {posture?.kill.active ? "ACTIVE" : "clear"}</TerminalBadge>
          <TerminalBadge tone={feedTone}>{`● feed ${posture?.feed.label ?? "unknown"}`}</TerminalBadge>
          <TerminalBadge tone="neutral">{who.data?.name ?? "…"} · {role}</TerminalBadge>
        </div>
      </div>
    </header>
  );
}

export function LiveBlockedBanner() {
  const { data } = useRiskSnapshot();
  if (!data?.live.blocked) return null;
  return (
    <div className="rounded-lg border border-short/50 bg-short/10 px-4 py-3 text-[12px] text-short" role="status">
      <strong>Live blocked.</strong> {data.live.message}
    </div>
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
  const { data } = useLanes();
  const lanes = data?.lanes ?? [];
  const cols: Column<CorrectionLane>[] = [
    { key: "strategy_id", header: "Strategy", render: (r) => <span className={r.eligibility === "KILLED" ? "text-faint line-through" : "font-mono"}>{r.strategy_id}</span> },
    {
      key: "eligibility", header: "Eligibility", render: (r) => (
        <TerminalBadge tone={r.eligibility === "eligible" ? "info" : r.eligibility === "KILLED" ? "bad" : r.eligibility === "RESEARCH_ONLY" ? "warn" : "neutral"}>{r.eligibility}</TerminalBadge>
      ),
    },
    { key: "mode", header: "Mode", render: (r) => <TerminalBadge tone={r.mode === "paper" ? "warn" : "neutral"}>{r.mode}</TerminalBadge> },
    { key: "market", header: "Symbol / TF", render: (r) => <span className="font-mono">{r.symbol || "—"} · {r.timeframe || "—"}</span> },
    { key: "capital", header: "Capital", render: (r) => <TerminalBadge tone={r.capital ? "bad" : "neutral"}>{r.capital ? "yes" : "no"}</TerminalBadge> },
    { key: "signal", header: "Last signal", render: (r) => r.last_signal_age_seconds == null ? "—" : ageSec(r.last_signal_age_seconds) },
    { key: "health", header: "Health", render: (r) => <TerminalBadge tone={r.health === "ok" ? "good" : r.health === "degraded" ? "bad" : "neutral"}>{r.health}</TerminalBadge> },
  ];
  return (
    <TerminalPanel title="Lanes" meta={`${lanes.length} active · policy truth · read only`}>
      {data?.banner && <div className="mb-4 rounded-lg border border-warn/40 bg-warn/5 px-3 py-2 text-[12px] text-warn">{data.banner}</div>}
      {lanes.length ? <DenseTable columns={cols} rows={lanes} /> : <div className="text-faint text-[12px] p-2">No lane telemetry.</div>}
    </TerminalPanel>
  );
}

// Verdict severity → badge tone. Mirrors lane_health.py: STALE/MISSING are
// hard-broken (bad); ORPHAN/SILENT/SHADOW_PROBATION are attention-not-dead (warn).
const HEALTH_TONE: Record<string, string> = {
  OK: "good",
  STALE: "bad",
  MISSING: "bad",
  SILENT: "warn",
  ORPHAN: "warn",
  SHADOW_PROBATION: "warn",
};

export function HealthPanel() {
  const { data } = useSnapshot();
  const lh = (data?.lane_health ?? null) as LaneHealth | null;
  const problems = lh?.problems ?? [];
  const cols: Column<LaneHealthProblem>[] = [
    { key: "lane_id", header: "Lane", render: (r) => r.lane_id ?? "—" },
    {
      key: "verdict",
      header: "Verdict",
      render: (r) => <TerminalBadge tone={(HEALTH_TONE[r.verdict ?? ""] ?? "neutral") as never}>{r.verdict ?? "—"}</TerminalBadge>,
    },
    { key: "age", header: "Last rec", render: (r) => ageSec(r.age_seconds) },
    { key: "detail", header: "Detail", render: (r) => <span className="text-dim">{r.detail ?? "—"}</span> },
  ];
  return (
    <TerminalPanel title="Lane health" meta={lh?.summary ?? "—"}>
      {!lh ? (
        <div className="text-faint text-[12px] p-2">No lane-health audit.</div>
      ) : problems.length === 0 ? (
        <div className="flex items-center gap-2 p-2">
          <TerminalBadge tone="good">all lanes OK</TerminalBadge>
          <span className="text-dim text-[12px]">{lh.production_summary ?? lh.summary}</span>
        </div>
      ) : (
        <DenseTable columns={cols} rows={problems} />
      )}
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
  const { data } = useRiskSnapshot();
  const journalBlocked = data?.journal.entries_blocked;
  return (
    <TerminalPanel title="Risk" meta="kill · halt · journal · gateway · streams">
      {journalBlocked && (
        <div className="mb-4 rounded-lg border border-short/50 bg-short/10 px-3 py-3 text-[12px] text-short">
          <strong>Journal degraded.</strong> New entries blocked until operator ack.
          {data?.journal.quarantine_path && <div className="mt-1 break-all font-mono text-[10px]">quarantine: {data.journal.quarantine_path}</div>}
        </div>
      )}
      <div className="flex items-center gap-3 flex-wrap">
        <TerminalBadge tone={data?.kill.active ? "bad" : "neutral"}>kill {data?.kill.active ? "ACTIVE" : "clear"}</TerminalBadge>
        <TerminalBadge tone={data?.daily_halt.active ? "bad" : "neutral"}>daily halt {data?.daily_halt.active ? "ACTIVE" : "clear"}</TerminalBadge>
        <TerminalBadge tone={data?.journal.available && !journalBlocked ? "good" : "bad"}>journal {data?.journal.available && !journalBlocked ? "healthy" : "blocked"}</TerminalBadge>
        <TerminalBadge tone="bad">Delta private: {data?.live.delta_private_status ?? "unknown"}</TerminalBadge>
      </div>
      <div className="flex items-end gap-10 flex-wrap my-5">
        <Kpi label="Daily loss used" value={usd(data?.daily_halt.used_usd)} />
        <Kpi label="Daily limit" value={usd(data?.daily_halt.limit_usd)} />
        <Kpi label="% peak equity" value={data?.daily_halt.used_pct_of_peak_equity == null ? "—" : `${data.daily_halt.used_pct_of_peak_equity.toFixed(2)}%`} />
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <div className="rounded-lg border border-line bg-inset p-3">
          <div className="mb-2 font-mono text-[10px] uppercase text-faint">Stream health</div>
          <div className="space-y-2">
            {(data?.streams ?? []).map((stream) => (
              <div key={stream.exchange} className="flex items-center justify-between gap-3 text-[11px]">
                <span className="font-mono">{stream.exchange}</span>
                <span className="text-dim">public {stream.public_feed} · private {stream.private_stream}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="rounded-lg border border-line bg-inset p-3">
          <div className="mb-2 font-mono text-[10px] uppercase text-faint">Gateway rejects</div>
          {(data?.gateway.last_reject_reasons ?? []).length ? data?.gateway.last_reject_reasons.map((item) => (
            <div key={item.reason} className="flex justify-between gap-3 text-[11px]"><span className="text-dim">{item.reason}</span><span className="font-mono">{item.count}</span></div>
          )) : <div className="text-[11px] text-dim">No recent reject reason in snapshot.</div>}
        </div>
      </div>
    </TerminalPanel>
  );
}

export function ResearchPanel() {
  const token = new URLSearchParams(window.location.search).get("token");
  const href = (path: string) => token ? `${path}?token=${encodeURIComponent(token)}` : path;
  const artifacts = [
    ["Strategy scorecard", "/scorecard"],
    ["OOS research evidence", "/research"],
    ["Pre-live checklist", "/pre-live-checklist"],
    ["Promotion review runbook", "/promotion-review-runbook"],
  ];
  return (
    <TerminalPanel title="Research" meta="evidence links only · no mutation">
      <div className="grid gap-3 md:grid-cols-2">
        {artifacts.map(([label, path]) => (
          <a key={path} href={href(path)} target="_blank" rel="noreferrer" className="flex items-center justify-between rounded-lg border border-line bg-inset px-4 py-3 text-[12px] hover:border-line2">
            <span>{label}</span><TerminalBadge tone="neutral">evidence only ↗</TerminalBadge>
          </a>
        ))}
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
