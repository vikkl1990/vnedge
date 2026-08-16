// Core cockpit panels, wired to the live endpoints via TanStack Query. This is
// the growing parity subset (header, book, risk, positions, journal); the
// remaining classic panels port onto these same primitives incrementally.

import { useEffect, useState } from "react";
import { DenseTable, TerminalBadge, TerminalPanel, type Column } from "../components/Terminal";
import { useAgenticResearchStatus, useCostModel, useJournal, useLanes, useMeta, useMlStatus, useResearchScorecard, useRiskSnapshot, useSnapshot, useWhoAmI } from "../queries";
import type { CorrectionLane, JournalRow, LaneHealth, LaneHealthProblem, LaneRow, PlanOverlay, Position, RegimeReading, Snapshot, TrialScorecard } from "../api";

const usd = (n: unknown) =>
  typeof n === "number" ? `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}` : "—";
const signed = (n: unknown) => (typeof n === "number" && n < 0 ? "text-short" : "text-long");

const ageSec = (s: unknown) => {
  if (s === null || s === undefined || s === "") return "—";
  const n = Number(s);
  if (!Number.isFinite(n)) return "—";
  return n < 90 ? `${Math.round(n)}s` : n < 5400 ? `${(n / 60).toFixed(1)}m` : `${(n / 3600).toFixed(1)}h`;
};

export function Header() {
  const who = useWhoAmI();
  const risk = useRiskSnapshot();
  const meta = useMeta();
  const costs = useCostModel();
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const posture = risk.data;
  const role = who.data?.role ?? "…";
  const feedTone = posture?.feed.status === "healthy" ? "good" : posture?.feed.status === "stale" ? "warn" : posture?.feed.status === "gap" ? "bad" : "neutral";
  const time = clock.toLocaleTimeString("en-GB", { hour12: false, timeZone: "UTC" });
  const feeWall = costs.data?.taker_round_trip_cost_bps;
  const sha = meta.data?.build_sha ?? posture?.build_sha;
  return (
    <header className="sticky top-0 z-30 -mx-2 rounded-xl border border-line bg-bg/95 px-4 py-3 shadow-xl shadow-black/20 backdrop-blur">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-md border border-brand/40 grid place-items-center text-brand font-mono">VN</div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-[15px] font-semibold">VNEDGE</span>
              <TerminalBadge tone="info">{posture?.runtime_mode ?? "syncing"}</TerminalBadge>
              <span className="hidden sm:inline text-[11px] font-mono text-dim">BTC · ETH · SOL</span>
            </div>
            <div className="text-[11px] font-mono text-dim">mode: {posture?.runtime_label ?? "…"}</div>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap justify-end">
          <span className="text-[11px] font-mono text-dim">{time} UTC</span>
          <TerminalBadge tone={posture?.capital.enabled ? "bad" : "neutral"}>capital {posture?.capital.enabled ? "ON" : "OFF"}</TerminalBadge>
          <TerminalBadge tone={posture?.kill.active ? "bad" : "neutral"}>kill {posture?.kill.active ? "ACTIVE" : "clear"}</TerminalBadge>
          <TerminalBadge tone={feedTone}>{`● feed ${posture?.feed.label ?? "unknown"}`}</TerminalBadge>
          <TerminalBadge tone="warn">fee wall {feeWall == null ? "—" : feeWall.toFixed(1)} bps</TerminalBadge>
          <TerminalBadge tone="neutral">build {sha ? sha.slice(0, 8) : "…"}</TerminalBadge>
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
  let risk: Band = s ? "ok" : "unknown";
  let rLabel = s ? "ok" : "no telemetry";
  const rs = String(s?.risk_status ?? "ok").toLowerCase();
  const streak = Number(s?.consecutive_losses) || 0;
  if (kill) { risk = "blocked"; rLabel = "kill tripped"; }
  else if (rs && rs !== "ok") { risk = "blocked"; rLabel = rs.slice(0, 14); }
  else if (streak >= 3) { risk = "degraded"; rLabel = `${streak} loss streak`; }
  // SYSTEM
  let system: Band = "unknown";
  let sLabel = "no telemetry";
  if (kill) { system = "blocked"; sLabel = "kill tripped"; }
  else if (s) {
    const components = [candle, decision, feed, risk];
    system = "ok";
    for (const x of components) {
      system = x === "unknown" ? worse(system, "degraded") : worse(system, x);
    }
    sLabel = system === "ok" ? "nominal" : system === "degraded" ? "partial telemetry" : "blocked";
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

export function DeskPanel() {
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
    { key: "market", header: "Symbol / TF", render: (r) => <span className="whitespace-nowrap font-mono">{r.symbol || "—"} · {r.timeframe || "—"}</span> },
    { key: "capital", header: "Capital", render: (r) => <TerminalBadge tone={r.capital ? "bad" : "neutral"}>{r.capital ? "yes" : "no"}</TerminalBadge> },
    { key: "rtt", header: "Venue RTT", align: "right", render: (r) => r.venue_rtt_ms == null ? "not reported" : `${r.venue_rtt_ms.toFixed(1)} ms` },
    { key: "candle", header: "Candle", render: (r) => <span className="whitespace-nowrap font-mono">{r.candle_status} · {r.candle_age_ms == null ? "age —" : ageSec(r.candle_age_ms / 1000)}</span> },
    { key: "lag", header: "Decision p95", align: "right", render: (r) => r.decision_lag_ms == null ? "—" : `${r.decision_lag_ms.toFixed(1)} ms` },
    { key: "skips", header: "Arm skips", align: "right", render: (r) => r.arm_skips.toLocaleString() },
    { key: "signal", header: "Last signal / reason", render: (r) => <span className="block min-w-[150px]"><span className="font-mono">{r.last_signal_age_seconds == null ? "—" : ageSec(r.last_signal_age_seconds)}</span><span className="block text-[10px] text-dim">{r.last_signal_reason}</span></span> },
    { key: "cost", header: "Cost profile", render: (r) => <span className="whitespace-nowrap font-mono">{r.cost_profile} · {r.round_trip_bps == null ? "RT —" : `${r.round_trip_bps.toFixed(1)} bps RT`}</span> },
    { key: "health", header: "Health", render: (r) => <TerminalBadge tone={r.health === "ok" ? "good" : r.health === "degraded" ? "bad" : "neutral"}>{r.health}</TerminalBadge> },
  ];
  return (
    <TerminalPanel title="Desk · runtime lanes" meta={`${lanes.length} active · policy truth · read only`}>
      {data?.banner && <div className="mb-4 rounded-lg border border-warn/40 bg-warn/5 px-3 py-2 text-[12px] text-warn">{data.banner}</div>}
      {lanes.length ? <DenseTable columns={cols} rows={lanes} /> : <div className="text-faint text-[12px] p-2">No lane telemetry.</div>}
      <div className="mt-4 rounded-lg border border-line bg-inset px-3 py-3">
        <div className="font-mono text-[10px] uppercase tracking-wider text-faint">Why no fire</div>
        <div className="mt-2 grid gap-1 text-[11px] text-dim md:grid-cols-2">
          {lanes.map((lane) => <div key={lane.lane_id}><span className="font-mono text-txt">{lane.lane_id}</span> · {lane.why_no_fire}</div>)}
          {!lanes.length && <div>No lane decision telemetry.</div>}
        </div>
      </div>
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
      <div className="grid grid-cols-2 gap-3 my-5 md:grid-cols-3 xl:grid-cols-6">
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Daily loss used" value={usd(data?.daily_halt.used_usd)} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Daily limit" value={usd(data?.daily_halt.limit_usd)} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="% peak equity" value={data?.daily_halt.used_pct_of_peak_equity == null ? "—" : `${data.daily_halt.used_pct_of_peak_equity.toFixed(2)}%`} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Open shadow" value={String(data?.positions.shadow_open ?? 0)} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Loss streak" value={`${data?.breaker.loss_streak ?? 0}/${data?.breaker.threshold ?? 3}`} tone={data?.breaker.active ? "text-short" : ""} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Unresolved" value={String(data?.positions.unresolved_orders ?? 0)} tone={(data?.positions.unresolved_orders ?? 0) > 0 ? "text-short" : ""} /></div>
      </div>
      <div className="grid gap-3 xl:grid-cols-3">
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
          )) : <div className="text-[11px] text-dim">No reject reason in the current snapshot.</div>}
          <div className="mt-2 border-t border-line pt-2 font-mono text-[10px] text-faint">{data?.gateway.observed_reject_count ?? 0} observed · {data?.gateway.window ?? "unknown window"}</div>
        </div>
        <div className="rounded-lg border border-line bg-inset p-3">
          <div className="mb-2 font-mono text-[10px] uppercase text-faint">Reconciliation</div>
          <div className="flex items-center justify-between text-[11px]"><span className="text-dim">status</span><TerminalBadge tone={data?.reconciliation.clean ? "good" : "neutral"}>{data?.reconciliation.status ?? "not reported"}</TerminalBadge></div>
          <div className="mt-2 flex items-center justify-between text-[11px]"><span className="text-dim">last success age</span><span className="font-mono">{ageSec(data?.reconciliation.last_success_age_seconds)}</span></div>
          <div className="mt-2 flex items-center justify-between text-[11px]"><span className="text-dim">fail count</span><span className="font-mono">{data?.reconciliation.fail_count ?? 0}</span></div>
        </div>
      </div>
      <div className="mt-4 rounded-lg border border-line bg-inset p-3">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="font-mono text-[10px] uppercase text-faint">Live gates</div>
          <TerminalBadge tone="bad">{data?.live_checklist.passed ?? 0}/{data?.live_checklist.total ?? 7}</TerminalBadge>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {(data?.live_checklist.items ?? []).map((item) => (
            <div key={item.id} className={`rounded-md border px-3 py-2 text-[11px] ${item.ok ? "border-long/30 text-long" : "border-short/30 text-short"}`}>
              {item.ok ? "✓" : "✕"} {item.label}
            </div>
          ))}
        </div>
      </div>
    </TerminalPanel>
  );
}

export function ResearchPanel() {
  const scorecard = useResearchScorecard();
  const [showUndersampled, setShowUndersampled] = useState(false);
  const token = new URLSearchParams(window.location.search).get("token");
  const href = (path: string) => token ? `${path}?token=${encodeURIComponent(token)}` : path;
  const artifacts = [
    ["Strategy scorecard", "/scorecard"],
    ["OOS research evidence", "/research"],
    ["Pre-live checklist", "/pre-live-checklist"],
    ["Promotion review runbook", "/promotion-review-runbook"],
  ];
  const scoreRows = (scorecard.data?.strategies ?? []).filter((row) => showUndersampled || row.samples >= 30);
  const scoreCols: Column<(typeof scoreRows)[number]>[] = [
    { key: "strategy", header: "Strategy", render: (r) => <span className="font-mono">{r.strategy}</span> },
    { key: "samples", header: "n", align: "right", render: (r) => <span className={r.samples < 30 ? "text-warn" : ""}>{r.samples}</span> },
    { key: "net", header: "Best net", align: "right", render: (r) => r.best_net_bps == null ? "—" : `${r.best_net_bps.toFixed(2)} bps` },
    { key: "pf", header: "PF", align: "right", render: (r) => r.profit_factor == null ? "—" : r.profit_factor.toFixed(2) },
    { key: "break", header: "Break rate", align: "right", render: (r) => r.break_rate_pct == null ? "—" : `${r.break_rate_pct.toFixed(1)}%` },
    { key: "verdict", header: "Evidence verdict", render: (r) => <TerminalBadge tone={String(r.verdict).toLowerCase().includes("pass") ? "info" : "neutral"}>{r.verdict ?? "unreported"}</TerminalBadge> },
  ];
  return (
    <div className="space-y-4">
      <TerminalPanel title="Research" meta="evidence only · no mutation">
      <div className="rounded-lg border border-line bg-inset p-4">
        <div className="font-mono text-[10px] uppercase tracking-wider text-faint">Pinned findings</div>
        <div className="mt-3 grid gap-3 md:grid-cols-3">
          <div><TerminalBadge tone="warn">fee wall</TerminalBadge><div className="mt-2 text-[12px]">Taker round trips remain structurally expensive; verify the live cost model above.</div></div>
          <div><TerminalBadge tone="bad">killed</TerminalBadge><div className="mt-2 text-[12px]">Fast directional and funding mean-reversion capital paths remain killed.</div></div>
          <div><TerminalBadge tone="info">open question</TerminalBadge><div className="mt-2 text-[12px]">Swing horizons may amortize the same tax, but require new pre-registered evidence.</div></div>
        </div>
      </div>
      <div className="mt-4 rounded-lg border border-line bg-inset p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div><div className="font-mono text-[10px] uppercase tracking-wider text-faint">Scorecard</div><div className="mt-1 text-[11px] text-dim">Production view requires n ≥ 30. Historical passes do not grant capital.</div></div>
          <label className="flex items-center gap-2 text-[11px] text-dim"><input type="checkbox" checked={showUndersampled} onChange={(event) => setShowUndersampled(event.target.checked)} /> show undersampled</label>
        </div>
        <DenseTable columns={scoreCols} rows={scoreRows} empty={scorecard.isLoading ? "loading evidence…" : "no sample-qualified scorecard rows"} />
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        {artifacts.map(([label, path]) => (
          <a key={path} href={href(path)} target="_blank" rel="noreferrer" className="flex items-center justify-between rounded-lg border border-line bg-inset px-4 py-3 text-[12px] hover:border-line2">
            <span>{label}</span><TerminalBadge tone="neutral">evidence only ↗</TerminalBadge>
          </a>
        ))}
      </div>
      </TerminalPanel>
      <IntelligencePanel />
    </div>
  );
}

function IntelligencePanel() {
  const ml = useMlStatus();
  const agents = useAgenticResearchStatus();
  const dataset = ml.data?.dataset;
  const summary = agents.data?.summary;
  const gateRows = Object.entries(ml.data?.gates ?? {}).slice(0, 6);
  const locked = !ml.data?.can_promote && !ml.data?.can_trade;
  const gateValue = (value: unknown) => {
    if (typeof value === "number") return value.toLocaleString();
    if (typeof value === "string" || typeof value === "boolean") return String(value);
    try { return JSON.stringify(value); } catch { return "unreported"; }
  };
  return (
    <TerminalPanel title="ML + research agents" meta="tertiary evidence · never order authority">
      <div className="grid gap-4 xl:grid-cols-2">
        <div className="rounded-lg border border-line bg-inset p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="font-mono text-[10px] uppercase tracking-wider text-faint">Meta-label pipeline</div>
              <div className="mt-1 text-[11px] text-dim">Scores rule outcomes after enough labels; it is not a free trader.</div>
            </div>
            <TerminalBadge tone={locked ? "warn" : "bad"}>{locked ? "gates locked" : "authority mismatch"}</TerminalBadge>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div><div className="text-[10px] font-mono text-faint">STAGE</div><div className="mt-1 text-[12px] font-mono">{ml.data?.stage ?? "unavailable"}</div></div>
            <div><div className="text-[10px] font-mono text-faint">LABELS</div><div className="mt-1 text-[12px] font-mono">{dataset ? `${dataset.samples}/${dataset.min_to_train}` : "—"}</div></div>
            <div><div className="text-[10px] font-mono text-faint">WIN RATE</div><div className="mt-1 text-[12px] font-mono">{dataset?.win_rate_pct == null ? "—" : `${dataset.win_rate_pct.toFixed(1)}%`}</div></div>
            <div><div className="text-[10px] font-mono text-faint">FEATURES</div><div className="mt-1 text-[12px] font-mono">{ml.data?.foundation.feature_count ?? "—"}</div></div>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            {(ml.data?.stages ?? []).map((stage) => (
              <TerminalBadge key={stage.key} tone={stage.done ? "neutral" : stage.active ? "info" : "warn"}>
                {stage.label} · {stage.done ? "done" : stage.active ? "active" : "locked"}
              </TerminalBadge>
            ))}
          </div>
          <div className="mt-4 border-t border-line pt-3">
            <div className="font-mono text-[10px] uppercase text-faint">Pre-registered gates</div>
            <div className="mt-2 grid gap-1 sm:grid-cols-2">
              {gateRows.map(([name, value]) => <div key={name} className="flex justify-between gap-3 text-[10px]"><span className="text-dim">{name}</span><span className="max-w-[55%] truncate font-mono">{gateValue(value)}</span></div>)}
              {!gateRows.length && <div className="text-[11px] text-dim">Gate artifact unavailable.</div>}
            </div>
          </div>
        </div>
        <div className="rounded-lg border border-line bg-inset p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="font-mono text-[10px] uppercase tracking-wider text-faint">Agent research governor</div>
              <div className="mt-1 text-[11px] text-dim">Ranks research tasks and stale evidence, never capital actions.</div>
            </div>
            <TerminalBadge tone="neutral">research only</TerminalBadge>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div><div className="text-[10px] font-mono text-faint">ACTIONS</div><div className="mt-1 text-[12px] font-mono">{summary?.operator_actions ?? 0}</div></div>
            <div><div className="text-[10px] font-mono text-faint">CRITICAL</div><div className="mt-1 text-[12px] font-mono text-short">{summary?.critical_actions ?? 0}</div></div>
            <div><div className="text-[10px] font-mono text-faint">ACTIVE TASKS</div><div className="mt-1 text-[12px] font-mono">{summary?.gateway_active_tasks ?? 0}</div></div>
            <div><div className="text-[10px] font-mono text-faint">MIN HEALTH</div><div className="mt-1 text-[12px] font-mono">{summary?.agent_health_min == null ? "—" : `${summary.agent_health_min.toFixed(0)}/100`}</div></div>
          </div>
          <div className="mt-4 rounded-md border border-line px-3 py-3 text-[11px] text-dim">
            {agents.data?.operator_answer ?? (agents.isError ? "Agent status endpoint unavailable." : "Agent artifact not populated.")}
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {(agents.data?.source_status ?? []).map((source) => (
              <TerminalBadge key={source.source} tone={source.state === "OK" ? "neutral" : source.state === "STALE" ? "warn" : "bad"}>
                {source.source ?? "source"} · {source.state ?? "unknown"}
              </TerminalBadge>
            ))}
          </div>
        </div>
      </div>
    </TerminalPanel>
  );
}

export function PromotePanel() {
  const risk = useRiskSnapshot();
  const lanes = useLanes();
  const scorecard = useResearchScorecard();
  const runtime = lanes.data?.lanes ?? [];
  const killed = runtime.filter((lane) => lane.eligibility === "KILLED");
  const researchOnly = runtime.filter((lane) => lane.eligibility === "RESEARCH_ONLY");
  const evidence = scorecard.data?.strategies ?? [];
  const sampleQualified = evidence.filter((row) => row.samples >= 30).length;
  const undersampled = evidence.filter((row) => row.samples < 30).length;
  return (
    <div className="space-y-4">
      <TerminalPanel title="Promote" meta="human ladder · no mutation controls">
        <div className="rounded-lg border border-short/40 bg-short/5 px-4 py-3 text-[12px] text-short">
          <strong>Capital remains off.</strong> Evidence, ML, and agents cannot add a strategy to the capital roster. Promotion requires reviewed code/config and operator attestation.
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Capital roster" value={String(risk.data?.capital.roster_size ?? 0)} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Live gates" value={`${risk.data?.live_checklist.passed ?? 0}/${risk.data?.live_checklist.total ?? 7}`} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Sample-qualified" value={String(sampleQualified)} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Undersampled" value={String(undersampled)} /></div>
        </div>
        <div className="mt-4 grid gap-4 xl:grid-cols-2">
          <div className="rounded-lg border border-line bg-inset p-4">
            <div className="mb-3 flex items-center justify-between"><div className="font-mono text-[10px] uppercase text-faint">Live checklist</div><TerminalBadge tone="bad">blocked</TerminalBadge></div>
            <div className="grid gap-2 sm:grid-cols-2">
              {(risk.data?.live_checklist.items ?? []).map((item) => <div key={item.id} className={`rounded-md border px-3 py-2 text-[11px] ${item.ok ? "border-long/30 text-long" : "border-short/30 text-short"}`}>{item.ok ? "✓" : "✕"} {item.label}</div>)}
            </div>
          </div>
          <div className="rounded-lg border border-line bg-inset p-4">
            <div className="font-mono text-[10px] uppercase text-faint">Registry boundary</div>
            <div className="mt-3 text-[11px] text-dim">Desk rows are the runtime roster. Research evidence is not merged into it and cannot become active through this screen.</div>
            <div className="mt-3 space-y-2">
              {killed.map((lane) => <div key={lane.lane_id} className="flex items-center justify-between gap-3 text-[11px]"><span className="font-mono text-faint line-through">{lane.strategy_id}</span><TerminalBadge tone="bad">sealed KILLED</TerminalBadge></div>)}
              {researchOnly.map((lane) => <div key={lane.lane_id} className="flex items-center justify-between gap-3 text-[11px]"><span className="font-mono">{lane.strategy_id}</span><TerminalBadge tone="warn">research only</TerminalBadge></div>)}
              {!runtime.length && <div className="text-[11px] text-dim">Runtime roster unavailable.</div>}
            </div>
          </div>
        </div>
      </TerminalPanel>
    </div>
  );
}

type FreshnessRow = { name: string; age: string; state: "OK" | "STALE" | "MISSING"; sla: string };

function generatedFreshness(name: string, generatedAt: string | null | undefined, slaSeconds: number): FreshnessRow {
  if (!generatedAt) return { name, age: "not reported", state: "MISSING", sla: ageSec(slaSeconds) };
  const parsed = Date.parse(generatedAt);
  if (!Number.isFinite(parsed)) return { name, age: "invalid timestamp", state: "MISSING", sla: ageSec(slaSeconds) };
  const seconds = Math.max(0, (Date.now() - parsed) / 1000);
  return { name, age: ageSec(seconds), state: seconds <= slaSeconds ? "OK" : "STALE", sla: ageSec(slaSeconds) };
}

export function SystemPanel() {
  const snapshot = useSnapshot();
  const risk = useRiskSnapshot();
  const meta = useMeta();
  const scorecard = useResearchScorecard();
  const ml = useMlStatus();
  const agents = useAgenticResearchStatus();
  const snapshotAge = typeof snapshot.data?.snapshot_age_ms === "number" ? snapshot.data.snapshot_age_ms / 1000 : null;
  const freshness: FreshnessRow[] = [
    snapshotAge == null
      ? { name: "runtime snapshot", age: "not reported", state: "MISSING", sla: "15s" }
      : { name: "runtime snapshot", age: ageSec(snapshotAge), state: snapshotAge <= 15 ? "OK" : "STALE", sla: "15s" },
    generatedFreshness("research scorecard", scorecard.data?.generated_at, 2 * 60 * 60),
    generatedFreshness("ML pipeline", ml.data?.generated_at, 2 * 60 * 60),
    generatedFreshness("agent governor", agents.data?.generated_at, 2 * 60 * 60),
  ];
  const staleCount = freshness.filter((row) => row.state !== "OK").length;
  const cols: Column<FreshnessRow>[] = [
    { key: "artifact", header: "Artifact", render: (row) => <span className="font-mono">{row.name}</span> },
    { key: "age", header: "Age", align: "right", render: (row) => row.age },
    { key: "sla", header: "SLA", align: "right", render: (row) => row.sla },
    { key: "state", header: "State", render: (row) => <TerminalBadge tone={row.state === "OK" ? "good" : row.state === "STALE" ? "warn" : "bad"}>{row.state}</TerminalBadge> },
  ];
  return (
    <div className="space-y-4">
      <TerminalPanel title="System" meta="freshness · feed · build · bad list">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Build" value={meta.data?.build_sha?.slice(0, 8) ?? "—"} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Host" value={meta.data?.host ?? "—"} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Uptime" value={ageSec(meta.data?.uptime_seconds)} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Non-OK artifacts" value={String(staleCount)} tone={staleCount ? "text-short" : ""} /></div>
        </div>
        <div className="mt-4 grid gap-4 xl:grid-cols-2">
          <div className="rounded-lg border border-line bg-inset p-4">
            <div className="mb-3 font-mono text-[10px] uppercase text-faint">Artifact freshness</div>
            <DenseTable columns={cols} rows={freshness} />
          </div>
          <div className="rounded-lg border border-line bg-inset p-4">
            <div className="mb-3 font-mono text-[10px] uppercase text-faint">Transport truth</div>
            <div className="space-y-2 text-[11px]">
              <div className="flex justify-between gap-3"><span className="text-dim">public feed</span><TerminalBadge tone={risk.data?.feed.status === "healthy" ? "good" : "bad"}>{risk.data?.feed.label ?? "unknown"}</TerminalBadge></div>
              {(risk.data?.streams ?? []).map((stream) => <div key={stream.exchange} className="flex justify-between gap-3"><span className="font-mono">{stream.exchange}</span><span className="text-dim">public {stream.public_feed} · private {stream.private_stream}</span></div>)}
              <div className="flex justify-between gap-3 border-t border-line pt-2"><span className="text-dim">Delta private</span><TerminalBadge tone={risk.data?.live.delta_private_status === "connected" ? "good" : "bad"}>{risk.data?.live.delta_private_status ?? "unknown"}</TerminalBadge></div>
            </div>
          </div>
        </div>
      </TerminalPanel>
      <HealthPanel />
      <FeedPanel />
    </div>
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
  const rows = data?.closed_trades ?? [];
  const summary = data?.summary;
  const decisionEvents = (data?.events ?? []).filter((row) => /reject|block|refus|risk|skip/i.test(`${row.event ?? ""} ${row.detail ?? ""}`));
  const decisionTimes = (data?.events ?? []).map((row) => row.ts).filter((value): value is string => Boolean(value)).sort();
  const lastDecisionTs = decisionTimes[decisionTimes.length - 1];
  const cols: Column<JournalRow>[] = [
    { key: "lane", header: "Lane", render: (r) => <span className="font-mono">{r.lane ?? r.symbol ?? "—"}</span> },
    { key: "side", header: "Side", render: (r) => r.side ?? "—" },
    {
      key: "pnl",
      header: "Net PnL",
      align: "right",
      render: (r) => {
        const net = r.net_pnl_usd ?? r.net_after_this_fill_fee_usd ?? r.virtual_net_usd;
        return <span className={signed(net)}>{usd(net)}</span>;
      },
    },
    { key: "exit", header: "Exit", render: (r) => <span className="text-dim">{r.exit_reason ?? r.resolution ?? "—"}</span> },
  ];
  return (
    <TerminalPanel title="Journal" meta={isLoading ? "loading…" : `${rows.length} closed · 20s`}>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Paper net" value={usd(summary?.actual_realized_pnl_usd)} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Fees" value={usd(summary?.fees_usd)} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Shadow net" value={usd(summary?.virtual_net_usd)} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Open orders" value={String(summary?.open_orders ?? 0)} /></div>
      </div>
      <div className="mt-4 grid gap-3 xl:grid-cols-2">
        <div className="rounded-lg border border-line bg-inset p-3">
          <div className="mb-2 font-mono text-[10px] uppercase text-faint">Decision rejects / arm blocks</div>
          {decisionEvents.slice(0, 8).map((event, index) => <div key={`${event.ts}-${index}`} className="border-t border-line/60 py-2 first:border-0 text-[11px]"><span className="font-mono text-txt">{event.event ?? "decision"}</span><span className="ml-2 text-dim">{event.detail ?? "no detail"}</span></div>)}
          {!decisionEvents.length && <div className="text-[11px] text-dim">No decision reject or arm-block records in the current journal window.</div>}
        </div>
        <div className="rounded-lg border border-line bg-inset p-3">
          <div className="font-mono text-[10px] uppercase text-faint">Decision log freshness</div>
          <div className="mt-2 text-[12px] text-dim">Last decision record</div>
          <div className="mt-1 font-mono text-lg">{lastDecisionTs ? ageSec((Date.now() - Date.parse(lastDecisionTs)) / 1000) : "not observed"}</div>
          <div className="mt-2 text-[10px] text-faint">An empty journal is explicit; it is not evidence of a healthy decision path.</div>
        </div>
      </div>
      <div className="mt-4"><DenseTable columns={cols} rows={rows} empty="no closed trades yet · waiting for append-only evidence" /></div>
    </TerminalPanel>
  );
}
