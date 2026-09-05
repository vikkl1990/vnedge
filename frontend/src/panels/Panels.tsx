// Core cockpit panels, wired to the live endpoints via TanStack Query. This is
// the growing parity subset (header, book, risk, positions, journal); the
// remaining classic panels port onto these same primitives incrementally.

import { useEffect, useMemo, useState } from "react";
import { DenseTable, TerminalBadge, TerminalPanel, type Column } from "../components/Terminal";
import { useAgenticResearchStatus, useBacktestLab, useCostModel, useDataProducts, useJournal, useLanes, useMeta, useMlStatus, useOperatorProfile, useReadiness, useResearchScorecard, useRiskSnapshot, useSnapshot, useStrategyWorkflow, useWhoAmI } from "../queries";
import { apiPost, type ArtifactMetadata, type BacktestDay, type BacktestJobAccepted, type BacktestMonth, type BacktestRunSummary, type BacktestTrade, type CorrectionLane, type JournalRow, type LaneHealth, type LaneHealthProblem, type Position } from "../api";

const usd = (n: unknown) =>
  typeof n === "number" ? `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}` : "—";
const signed = (n: unknown) => (typeof n === "number" && n < 0 ? "text-short" : "text-long");
const priceText = (value: unknown) => {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const maximumFractionDigits = value >= 1_000 ? 2 : value >= 1 ? 4 : 8;
  return new Intl.NumberFormat("en-US", { maximumFractionDigits }).format(value);
};

const ageSec = (s: unknown) => {
  if (s === null || s === undefined || s === "") return "—";
  const n = Number(s);
  if (!Number.isFinite(n)) return "—";
  return n < 90 ? `${Math.round(n)}s` : n < 5400 ? `${(n / 60).toFixed(1)}m` : `${(n / 3600).toFixed(1)}h`;
};

export function Header() {
  const who = useWhoAmI();
  const profile = useOperatorProfile();
  const risk = useRiskSnapshot();
  const meta = useMeta();
  const costs = useCostModel();
  const lanes = useLanes();
  const snapshot = useSnapshot();
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const posture = risk.data;
  const riskUnknown = !posture;
  const riskUnavailable = risk.isError || (!risk.isLoading && riskUnknown);
  const role = who.data?.role ?? "…";
  const feedTone = riskUnknown ? "bad" : posture.feed.status === "healthy" ? "good" : posture.feed.status === "stale" ? "warn" : posture.feed.status === "gap" ? "bad" : "neutral";
  const systemChip = snapshot.data?.chips?.SYSTEM;
  const systemBand = (systemChip?.band ?? "unknown") as Band;
  const time = clock.toLocaleTimeString("en-GB", { hour12: false, timeZone: "UTC" });
  const preferredTimezone = profile.data?.timezone || "UTC";
  const localTime = preferredTimezone === "UTC" ? null : clock.toLocaleTimeString("en-GB", { hour12: false, timeZone: preferredTimezone });
  const displayName = profile.data?.display_name || who.data?.name || "…";
  const feeWall = costs.data?.taker_round_trip_cost_bps;
  const sha = meta.data?.build_sha ?? posture?.build_sha;
  const shadowPurse = lanes.data?.portfolio.shadow_purse_usd;
  const margin = lanes.data?.lanes.find((lane) => lane.observation_class === "shadow_observe")?.sizing_profile?.fixed_margin_usd;
  const leverage = lanes.data?.lanes.find((lane) => lane.observation_class === "shadow_observe")?.sizing_profile?.max_leverage;
  return (
    <header className="relative z-10 rounded-md border border-line bg-bg/95 px-3 py-2 shadow-lg shadow-black/20 backdrop-blur">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-md border border-brand/40 grid place-items-center text-brand font-mono">VN</div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-[15px] font-semibold">VNEDGE</span>
              <TerminalBadge tone={riskUnavailable ? "bad" : "info"}>{riskUnavailable ? "UNKNOWN" : posture?.runtime_mode ?? "syncing"}</TerminalBadge>
              <span className="hidden sm:inline text-[11px] font-mono text-dim">BTC · ETH · SOL</span>
            </div>
            <div className="text-[11px] font-mono text-dim">mode: {posture?.runtime_label ?? "…"}</div>
          </div>
        </div>
        <div className="flex items-center gap-1.5 flex-wrap justify-end">
          <span className="hidden text-[11px] font-mono text-dim sm:inline">{time} UTC{localTime ? ` · ${localTime} ${preferredTimezone}` : ""}</span>
          <TerminalBadge tone={BAND_TONE[systemBand] as never}>system {systemChip?.label ?? "unknown"}</TerminalBadge>
          <TerminalBadge tone={riskUnknown || posture?.capital.enabled ? "bad" : "neutral"}>capital {riskUnknown ? "UNKNOWN" : posture.capital.enabled ? "ON" : "OFF"}</TerminalBadge>
          <TerminalBadge tone={riskUnknown || posture?.kill.active ? "bad" : "neutral"}>kill {riskUnknown ? "UNKNOWN" : posture.kill.active ? "ACTIVE" : "clear"}</TerminalBadge>
          <TerminalBadge tone={feedTone}>{`● public feed ${posture?.feed.label ?? "unknown"}`}</TerminalBadge>
          <span className="hidden items-center gap-1.5 xl:inline-flex">
            <TerminalBadge tone="info">shadow {shadowPurse == null ? "—" : usd(shadowPurse)} · {margin == null ? "—" : usd(margin)} margin · ≤{leverage ?? "—"}x</TerminalBadge>
            <TerminalBadge tone="warn">fee wall {feeWall == null ? "—" : feeWall.toFixed(1)} bps</TerminalBadge>
            <TerminalBadge tone="neutral">build {sha ? sha.slice(0, 8) : "…"}</TerminalBadge>
            <TerminalBadge tone="neutral">{displayName} · {role}</TerminalBadge>
          </span>
          <details className="relative xl:hidden">
            <summary className="cursor-pointer list-none rounded-md border border-line px-2 py-[2px] text-[11px] font-mono uppercase text-dim">more</summary>
            <div className="absolute right-0 top-7 z-30 flex min-w-[260px] flex-col gap-2 rounded-lg border border-line bg-bg p-3 shadow-xl">
              <span className="font-mono text-[10px] text-faint sm:hidden">{time} UTC{localTime ? ` · ${localTime} ${preferredTimezone}` : ""}</span>
              <TerminalBadge tone="info">shadow {shadowPurse == null ? "—" : usd(shadowPurse)} · {margin == null ? "—" : usd(margin)} margin · ≤{leverage ?? "—"}x</TerminalBadge>
              <TerminalBadge tone="warn">fee wall {feeWall == null ? "—" : feeWall.toFixed(1)} bps</TerminalBadge>
              <TerminalBadge tone="neutral">build {sha ? sha.slice(0, 8) : "…"}</TerminalBadge>
              <TerminalBadge tone="neutral">{displayName} · {role}</TerminalBadge>
            </div>
          </details>
        </div>
      </div>
    </header>
  );
}

export function LiveBlockedBanner() {
  const { data, isLoading, isError } = useRiskSnapshot();
  if (isLoading && !data) {
    return (
      <div className="rounded-lg border border-warn/50 bg-warn/10 px-4 py-3 text-[12px] text-warn" role="status">
        <strong>Live status syncing.</strong> Treat live as blocked until risk telemetry arrives.
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div className="rounded-lg border border-short/50 bg-short/10 px-4 py-3 text-[12px] text-short" role="alert">
        <strong>Live status unknown.</strong> Risk backend is unreachable; live must be treated as blocked.
      </div>
    );
  }
  if (!data?.live.blocked) return null;
  return (
    <div className="flex items-center gap-3 border-l-[3px] border-warn bg-warn/5 px-3 py-2 text-[11px] text-dim" role="status">
      <TerminalBadge tone="warn">capital path locked</TerminalBadge><span>{data.live.message}</span>
    </div>
  );
}

export function ReadinessBanner() {
  const readiness = useReadiness();
  if (readiness.isLoading && !readiness.data) return null;
  if (readiness.isError || !readiness.data || readiness.data.status === "unknown") {
    return <div className="border-l-[3px] border-short bg-short/5 px-3 py-2 text-[11px] text-short" role="alert"><strong>Workflow readiness unknown.</strong> Canonical data and lane readiness could not be verified.</div>;
  }
  if (readiness.data.status === "ready") return null;
  const reasons = readiness.data.reasons.map((reason) => reason.replace(/_/g, " ")).join(" · ");
  return <div className="border-l-[3px] border-short bg-short/5 px-3 py-2 text-[11px] text-short" role="alert"><strong>Workflow not ready.</strong> {reasons || `HTTP ${readiness.data.http_status}`}. Live ticks may still be flowing; closed-candle and decision data must be treated as degraded.</div>;
}

// Health bands are server policy truth. Missing bands stay UNKNOWN; the client
// must not independently reinterpret latency or cumulative arm-skip counters.
type Band = "ok" | "degraded" | "blocked" | "unknown";
const BAND_TONE: Record<Band, string> = { ok: "good", degraded: "warn", blocked: "bad", unknown: "neutral" };

const UNKNOWN_CHIPS: Record<string, { band: Band; label: string }> = Object.fromEntries(
  ["SYSTEM", "FEED", "CANDLE", "DECISION", "RISK"].map((name) => [name, { band: "unknown" as const, label: "no telemetry" }]),
);

const BAND_BORDER: Record<Band, string> = {
  ok: "border-l-long",
  degraded: "border-l-warn",
  blocked: "border-l-short",
  unknown: "border-l-line",
};

export function StatusStrip() {
  const { data } = useSnapshot();
  // health_bands.py is the only policy source for both cockpits.
  const server = data?.chips as Record<string, { band: Band; label: string }> | undefined;
  const chips = server && Object.keys(server).length ? server : UNKNOWN_CHIPS;
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-1.5">
      {Object.entries(chips).map(([name, c]) => (
        <div
          key={name}
          className={`flex items-center gap-2 border border-line border-l-[3px] ${BAND_BORDER[c.band]} bg-inset px-2.5 py-1.5`}
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
  const setupTone = (state: CorrectionLane["lifecycle"]["state"]): "good" | "info" | "warn" | "bad" | "neutral" => (
    state === "accepted" || state === "holding" ? "good"
      : state === "armed" ? "info"
      : state === "session_blocked" ? "neutral"
      : state === "degraded" ? "bad"
      : "neutral"
  );
  const cols: Column<CorrectionLane>[] = [
    {
      key: "strategy_id", header: "Lane", render: (r) => <span className="block min-w-[190px]"><span className={r.eligibility === "KILLED" ? "font-mono text-faint line-through" : "font-mono text-txt"}>{r.strategy_id}</span><span className="block text-[9px] text-faint">{r.eligibility} · {r.observation_class === "shadow_observe" ? "SHADOW_OBSERVE" : r.mode}</span></span>,
    },
    { key: "market", header: "Market", render: (r) => <span className="whitespace-nowrap font-mono">{r.symbol || "—"}<span className="block text-[9px] text-faint">decision {r.timeframe || "—"}</span></span> },
    { key: "engine", header: "Entry engine", render: (r) => <span className="block whitespace-nowrap font-mono">{r.lifecycle.engine_kind === "quote_acceptance" ? "BBO accept" : r.lifecycle.engine_kind === "maker_retest" ? "maker retest" : r.lifecycle.engine_kind === "taker" ? "taker now" : r.lifecycle.engine_kind === "next_open" ? "next open" : "measurement"}<span className="block text-[9px] text-faint">path {r.path_id}</span><span className="block text-[9px] text-faint">{r.lifecycle.fill_evidence?.replace(/_/g, " ") ?? `protect ${r.runtime_contract?.protection_clock ?? "—"}`}</span></span> },
    { key: "state", header: "Setup state", render: (r) => <span className="block min-w-[105px]"><TerminalBadge tone={setupTone(r.lifecycle.state)}>{r.lifecycle.state}</TerminalBadge><span className="mt-1 block max-w-[150px] truncate font-mono text-[9px] text-faint" title={r.lifecycle.arm_state ?? r.current_waiting_reason}>{r.lifecycle.arm_state ?? r.current_waiting_reason}</span></span> },
    { key: "readiness", header: "Ready D/S/P/X/L", render: (r) => {
      const ready = r.runtime_readiness;
      if (!ready) return <span className="font-mono text-faint">— / — / — / — / —</span>;
      const blockers = ready.live_blockers.join(" · ") || "all runtime stages ready";
      const mark = (ok: boolean, warn = false) => <span className={ok ? "text-long" : warn ? "text-warn" : "text-short"}>{ok ? "✓" : "✕"}</span>;
      return <span className="block min-w-[135px] font-mono" title={blockers}>{mark(ready.data_ready)} / {mark(ready.decision_ready)} / {mark(ready.parity_ready, true)} / {mark(ready.execution_ready, true)} / {mark(ready.live_ready)}<span className="mt-1 block max-w-[165px] truncate text-[9px] text-faint">{ready.live_blockers[0] ?? "ready"}</span></span>;
    } },
    { key: "funnel", header: "Lifecycle", render: (r) => <span className="block min-w-[180px] font-mono"><span className="block">arm {r.lifecycle.armed_entries} → cand {r.lifecycle.candidates} → acc {r.lifecycle.accepted}</span><span className="block text-[9px] text-faint">rej {r.lifecycle.rejected}: cost {r.lifecycle.cost_rejected} · size {r.lifecycle.sizing_rejected} · risk {r.lifecycle.risk_rejected} · {r.lifecycle.fires == null ? "fires n/a" : `fires ${r.lifecycle.fires}`}</span></span> },
    { key: "context", header: "Session / HTF", render: (r) => <span className="block min-w-[100px] font-mono">{r.lifecycle.session_state}<span className="block text-[9px] text-faint">HTF {r.lifecycle.htf_context_age_seconds == null ? "n/a" : ageSec(r.lifecycle.htf_context_age_seconds)}</span></span> },
    { key: "close-lag", header: "Close → arm p95", align: "right", render: (r) => <span title={`${r.latency_samples.bar_close}/${r.latency_samples.required} receipt samples`}><span className="block">{r.close_to_arm_ms == null ? "—" : `${r.close_to_arm_ms.toFixed(1)} ms`}</span><span className="block text-[9px] text-faint">receipt {r.bar_close_receipt_ms == null ? "—" : `${r.bar_close_receipt_ms.toFixed(0)} ms`} · wait {r.canonical_wait_ms == null ? "—" : `${r.canonical_wait_ms.toFixed(0)} ms`}</span></span> },
    { key: "waiting", header: "Why waiting", render: (r) => <span className="block min-w-[165px]"><span className="font-mono text-dim">{r.drought?.drought_class?.replace(/_/g, " ") ?? r.current_waiting_reason}</span><span className="block text-[9px] text-faint">eval {r.drought?.eval_age_s == null ? "n/a" : ageSec(r.drought.eval_age_s)} · evidence {r.drought?.evidence_age_s == null ? "none" : ageSec(r.drought.evidence_age_s)}</span><span className="block text-[9px] text-faint">{r.drought?.last_primary_failed_gate ?? `${r.arm_skips.toLocaleString("en-US")} arm skips`}</span></span> },
    { key: "virtual", header: "Shadow booked", align: "right", render: (r) => r.lifecycle.net_unit !== "USD" ? "—" : <span className="block min-w-[105px] font-mono">{usd(r.lifecycle.net_value)}<span className="block text-[9px] text-faint">USD · {r.lifecycle.resolved} resolved · {r.lifecycle.pending} pending</span></span> },
    { key: "cost", header: "Gate wall", render: (r) => <span className="whitespace-nowrap font-mono">{r.cost_profile}<span className="block text-[9px] text-faint">{r.round_trip_bps == null ? "not reported" : `${r.round_trip_bps.toFixed(1)} bps RT`}</span></span> },
    { key: "health", header: "Ops health", render: (r) => {
      const reasons = r.health_reasons?.length ? r.health_reasons : r.health_reason ? [r.health_reason] : [];
      const close = r.health_details?.bar_close_receipt;
      const decision = r.health_details?.decision_compute;
      const title = [
        reasons.join(" · ") || "operationally nominal",
        close?.p95_ms == null ? "" : `close receipt p95 ${close.p95_ms.toFixed(1)}ms (soft ${close.soft_ms} / hard ${close.hard_ms}; n=${close.samples})`,
        decision?.p95_ms == null ? "" : `decision p95 ${decision.p95_ms.toFixed(1)}ms (soft ${decision.soft_ms} / hard ${decision.hard_ms}; n=${decision.samples})`,
      ].filter(Boolean).join("\n");
      return <span className="block min-w-[150px]"><TerminalBadge tone={r.health === "ok" ? "good" : r.health === "degraded" ? "warn" : r.health === "blocked" ? "bad" : "neutral"}>{r.health}</TerminalBadge><span className="mt-1 block max-w-[180px] text-[9px] leading-4 text-faint" title={title}>{reasons.length ? reasons.slice(0, 2).join(" · ") : "operationally nominal"}</span>{reasons.length > 2 && <span className="block text-[9px] text-faint">+{reasons.length - 2} more</span>}</span>;
    } },
  ];
  return (
    <TerminalPanel title="Desk · runtime lanes" meta={`${lanes.length} active · policy truth · read only`}>
      {data?.snapshot_state !== "fresh" && <div className="mb-4 rounded-lg border border-short/50 bg-short/10 px-3 py-2 text-[12px] text-short" role="alert"><strong>Lane snapshot {data?.snapshot_state ?? "unknown"}.</strong> Setup states and counters are not live. Age {data?.snapshot_age_ms == null ? "not reported" : ageSec(data.snapshot_age_ms / 1000)}; SLA {data?.snapshot_sla_ms == null ? "—" : ageSec(data.snapshot_sla_ms / 1000)}.</div>}
      {data?.banner && <div className="mb-4 rounded-lg border border-warn/40 bg-warn/5 px-3 py-2 text-[12px] text-warn">{data.banner}</div>}
      {lanes.length ? <DenseTable columns={cols} rows={lanes} rowKey={(lane) => lane.lane_id} /> : <div className="text-faint text-[12px] p-2">No lane telemetry.</div>}
      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {lanes.map((lane) => (
          <details key={`${lane.lane_id}-detail`} className="rounded-lg border border-line bg-inset px-3 py-3">
            <summary className="cursor-pointer list-none text-[11px] font-mono"><span className="text-txt">{lane.symbol} · {lane.timeframe}</span><span className="float-right text-dim">inspect lane ▾</span></summary>
            <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-[10px]">
              <span className="text-faint">virtual purse</span><span className="text-right font-mono">{usd(lane.sizing_profile?.starting_equity_usd ?? lane.equity_usd)}</span>
              <span className="text-faint">margin / leverage</span><span className="text-right font-mono">{usd(lane.sizing_profile?.fixed_margin_usd)} / ≤{lane.sizing_profile?.max_leverage ?? "—"}x</span>
              <span className="text-faint">bars / evaluations</span><span className="text-right font-mono">{lane.funnel.bars ?? 0} / {lane.funnel.evals ?? 0}</span>
              <span className="text-faint">armed / candidate / accepted</span><span className="text-right font-mono">{lane.lifecycle.armed_entries} / {lane.lifecycle.candidates} / {lane.lifecycle.accepted}</span>
              <span className="text-faint">rejects total / cost</span><span className="text-right font-mono">{lane.lifecycle.rejected} / {lane.lifecycle.cost_rejected}</span>
              <span className="text-faint">size / risk / portfolio</span><span className="text-right font-mono">{lane.lifecycle.sizing_rejected} / {lane.lifecycle.risk_rejected} / {lane.lifecycle.portfolio_rejected}</span>
              <span className="text-faint">fires</span><span className="text-right font-mono">{lane.lifecycle.fires == null ? "n/a · BBO entry" : lane.lifecycle.fires}</span>
              <span className="text-faint">positions / pending</span><span className="text-right font-mono">{lane.open_positions} / {lane.shadow_perf?.pending_shadow_intents ?? 0}</span>
              <span className="text-faint">decision engine</span><span className="text-right font-mono break-all">{lane.runtime_contract?.decision_engine ?? "unreported"}</span>
              <span className="text-faint">execution route</span><span className="text-right font-mono break-all">{lane.entry_route}{lane.entry_route === "maker_retest" ? ` · ${lane.maker_fill_ttl_bars ?? "—"} bars TTL` : ""}</span>
              <span className="text-faint">kernel path</span><span className="text-right font-mono break-all">{lane.path_id}</span>
              <span className="text-faint">permission snapshot</span><span className="text-right font-mono break-all">{lane.permission_snapshot_id ?? "not armed"}</span>
              <span className="text-faint">fill evidence</span><span className="text-right font-mono break-all">{lane.lifecycle.fill_evidence?.replace(/_/g, " ") ?? "not route-specific"}</span>
              <span className="text-faint">clock contract</span><span className="text-right font-mono break-all">{lane.runtime_contract?.decision_tf ?? lane.timeframe} close → {lane.runtime_contract?.entry_clock === "bbo_acceptance" ? "BBO" : lane.runtime_contract?.entry_clock === "execution_route" ? lane.entry_route : "next open"} → {lane.runtime_contract?.protection_clock ?? "ticks"}</span>
              <span className="text-faint">last-closed context</span><span className="text-right font-mono break-all">{(lane.runtime_contract?.context_tfs ?? []).map((tf) => `${tf} ${ageSec(lane.runtime_contract?.context_age_seconds?.[tf])}`).join(" · ") || "none"}</span>
              <span className="text-faint">operational blockers</span><span className="text-right font-mono break-all">{lane.health_reasons?.join(" · ") || "none"}</span>
              <span className="text-faint">data / signal / parity / execute / live</span><span className="text-right font-mono break-all">{lane.runtime_readiness ? `${lane.runtime_readiness.data_ready ? "ready" : "blocked"} / ${lane.runtime_readiness.decision_ready ? "ready" : "blocked"} / ${lane.runtime_readiness.parity_ready ? "ready" : "blocked"} / ${lane.runtime_readiness.execution_ready ? "ready" : "blocked"} / ${lane.runtime_readiness.live_ready ? "ready" : "blocked"}` : "not reported"}</span>
              <span className="text-faint">execution blockers</span><span className="text-right font-mono break-all">{lane.runtime_readiness?.execution_blockers.join(" · ") || "none"}</span>
              <span className="text-faint">close receipt p95</span><span className="text-right font-mono">{lane.health_details?.bar_close_receipt?.p95_ms == null ? "—" : `${lane.health_details.bar_close_receipt.p95_ms.toFixed(1)} ms`} / hard {lane.health_details?.bar_close_receipt?.hard_ms ?? "—"} ms</span>
              <span className="text-faint">decision compute p95</span><span className="text-right font-mono">{lane.health_details?.decision_compute?.p95_ms == null ? "—" : `${lane.health_details.decision_compute.p95_ms.toFixed(1)} ms`} / hard {lane.health_details?.decision_compute?.hard_ms ?? "—"} ms</span>
              <span className="text-faint">exit engine</span><span className="text-right font-mono break-all">{lane.runtime_contract?.exit_engine ?? "unreported"}</span>
              <span className="text-faint">last evaluation</span><span className="text-right font-mono break-all">{String(lane.last_eval?.reason ?? lane.last_signal_reason)}</span>
            </div>
          </details>
        ))}
      </div>
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
      {lh?.totals && <div className="mb-4 flex flex-wrap gap-2">{Object.entries(lh.totals).map(([label, count]) => <TerminalBadge key={label} tone={label === "OK" ? "good" : count > 0 ? (HEALTH_TONE[label] ?? "warn") as never : "neutral"}>{label} {count}</TerminalBadge>)}</div>}
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
  const { data: snapshot, isLoading, isError } = useSnapshot();
  const lanes = useLanes();
  const scope = lanes.data?.portfolio;
  const shadowNet = (lanes.data?.lanes ?? []).reduce((sum, lane) => sum + (lane.shadow_perf?.virtual_net_usd ?? 0), 0);
  return (
    <TerminalPanel title="Book · scoped capital" meta={isLoading ? "loading…" : isError ? "error" : "virtual vs nominal kept separate · 5s"}>
      {isError && <div className="mb-4 rounded-md border border-short/40 bg-short/5 px-3 py-2 text-[11px] text-short" role="alert">Account snapshot unavailable. Equity and PnL are unknown.</div>}
      <div className="flex items-end gap-10 flex-wrap">
        <Kpi label="Shadow purse" value={usd(scope?.shadow_purse_usd)} />
        <Kpi label="Shadow net" value={usd(shadowNet)} tone={signed(shadowNet)} />
        <Kpi label="Paper purse" value={usd(scope?.paper_purse_usd)} />
        <Kpi label="Measurement nominal" value={usd(scope?.measurement_nominal_usd)} />
      </div>
      <div className="mt-4 text-[10px] text-faint">Primary-lane snapshot equity {usd(snapshot?.equity)} is diagnostic only and is no longer presented as the scanner purse.</div>
    </TerminalPanel>
  );
}

export function RiskPanel() {
  const { data, isLoading, isError, error } = useRiskSnapshot();
  if (isLoading && !data) {
    return (
      <TerminalPanel title="Risk" meta="loading risk telemetry…">
        <div className="rounded-lg border border-warn/40 bg-warn/5 p-5 font-mono text-sm text-warn">
          Risk state is loading. Capital, kill, halt, journal, and reconciliation are unknown.
        </div>
      </TerminalPanel>
    );
  }
  if (isError || !data) {
    return (
      <TerminalPanel title="Risk" meta="backend unreachable · fail visible">
        <div className="rounded-lg border border-short/50 bg-short/10 p-5 text-short" role="alert">
          <strong>Risk telemetry unavailable.</strong>
          <div className="mt-2 text-[12px] text-dim">
            Kill, daily halt, journal health, unresolved orders, reconciliation, and live gates are UNKNOWN. Treat new risk and live operation as blocked.
          </div>
          <div className="mt-3 font-mono text-[10px] text-faint">
            {error instanceof Error ? error.message : "risk snapshot request failed"}
          </div>
        </div>
      </TerminalPanel>
    );
  }
  const journalBlocked = data?.journal.entries_blocked;
  const dailyRemaining = data.daily_halt.limit_usd == null
    ? null
    : Math.max(0, data.daily_halt.limit_usd - data.daily_halt.used_usd);
  const dailyUsedPct = data.daily_halt.limit_usd && data.daily_halt.limit_usd > 0
    ? Math.min(100, Math.max(0, data.daily_halt.used_usd / data.daily_halt.limit_usd * 100))
    : 0;
  const configuredPositionCap = Math.max(0, ...data.sizing_profiles.map((profile) => profile.max_open_positions ?? 0));
  const positionUsedPct = configuredPositionCap > 0 ? Math.min(100, data.positions.shadow_open / configuredPositionCap * 100) : 0;
  return (
    <TerminalPanel title="Risk" meta="kill · halt · journal · gateway · streams">
      {journalBlocked && (
        <div className="mb-4 rounded-lg border border-short/50 bg-short/10 px-3 py-3 text-[12px] text-short">
          <strong>Journal degraded.</strong> New entries blocked until operator ack.
          {data?.journal.quarantine_path && <div className="mt-1 break-all font-mono text-[10px]">quarantine: {data.journal.quarantine_path}</div>}
        </div>
      )}
      {data.breaker.active && (
        <div className="mb-4 rounded-lg border border-short/50 bg-short/10 px-3 py-3 text-[12px] text-short" role="alert">
          <strong>Consecutive-loss breaker active.</strong> New entries are blocked after {data.breaker.loss_streak} losses (threshold {data.breaker.threshold}); reduce-only exits remain available.
        </div>
      )}
      <div className="flex items-center gap-3 flex-wrap">
        <TerminalBadge tone={data?.kill.active ? "bad" : "neutral"}>kill {data?.kill.active ? "ACTIVE" : "clear"}</TerminalBadge>
        <TerminalBadge tone={data?.daily_halt.active ? "bad" : "neutral"}>daily halt {data?.daily_halt.active ? "ACTIVE" : "clear"}</TerminalBadge>
        <TerminalBadge tone={data?.journal.available && !journalBlocked ? "good" : "bad"}>journal {data?.journal.available && !journalBlocked ? "healthy" : "blocked"}</TerminalBadge>
        <TerminalBadge tone="bad">Delta private: {data?.live.delta_private_status ?? "unknown"}</TerminalBadge>
      </div>
      <div className="grid grid-cols-2 gap-3 my-5 md:grid-cols-3 xl:grid-cols-6">
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Shadow purse" value={usd(data.portfolio.shadow_purse_usd)} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Pending intents" value={String(data.positions.shadow_pending_intents)} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Daily loss used / left" value={`${usd(data.daily_halt.used_usd)} / ${dailyRemaining == null ? "—" : usd(dailyRemaining)}`} tone={data.daily_halt.active ? "text-short" : ""} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Open shadow" value={String(data?.positions.shadow_open ?? 0)} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Loss streak" value={`${data?.breaker.loss_streak ?? 0}/${data?.breaker.threshold ?? 3}`} tone={data?.breaker.active ? "text-short" : ""} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Unresolved" value={String(data?.positions.unresolved_orders ?? 0)} tone={(data?.positions.unresolved_orders ?? 0) > 0 ? "text-short" : ""} /></div>
      </div>
      <div className="mb-4 grid gap-3 md:grid-cols-2">
        <div className="border border-line bg-inset p-3">
          <div className="flex items-center justify-between font-mono text-[10px]"><span className="uppercase text-faint">Daily loss budget</span><span>{dailyUsedPct.toFixed(1)}% used</span></div>
          <div className="mt-2 h-2 bg-line/60"><div className={`h-full ${dailyUsedPct >= 80 ? "bg-short" : dailyUsedPct >= 50 ? "bg-warn" : "bg-long"}`} style={{ width: `${dailyUsedPct}%` }} /></div>
          <div className="mt-2 text-[10px] text-dim">{usd(data.daily_halt.used_usd)} consumed · {dailyRemaining == null ? "limit not reported" : `${usd(dailyRemaining)} available`} · hard halt remains server-owned</div>
        </div>
        <div className="border border-line bg-inset p-3">
          <div className="flex items-center justify-between font-mono text-[10px]"><span className="uppercase text-faint">Position capacity</span><span>{data.positions.shadow_open}/{configuredPositionCap || "—"}</span></div>
          <div className="mt-2 h-2 bg-line/60"><div className={`h-full ${positionUsedPct >= 80 ? "bg-short" : positionUsedPct >= 50 ? "bg-warn" : "bg-info"}`} style={{ width: `${positionUsedPct}%` }} /></div>
          <div className="mt-2 text-[10px] text-dim">{data.positions.shadow_pending_intents} pending · {data.positions.unresolved_orders} unresolved · measurement/shadow only</div>
        </div>
      </div>
      <div className="mb-4 rounded-lg border border-line bg-inset p-3">
        <div className="mb-3 font-mono text-[10px] uppercase text-faint">Sizing and exposure contracts</div>
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {data.sizing_profiles.map((profile) => {
            const ticketNotional = (profile.fixed_margin_usd ?? 0) * (profile.max_leverage ?? 0);
            const cap = profile.max_symbol_exposure_usd ?? profile.max_total_exposure_usd ?? 0;
            const configuredPct = cap > 0 ? Math.min(100, ticketNotional / cap * 100) : 0;
            return <div key={profile.lane_id} className="border border-line px-3 py-2 text-[10px]"><div className="flex items-center justify-between"><span className="font-mono text-txt">{profile.symbol}</span><span className="font-mono text-faint">ticket ≤ {usd(ticketNotional)}</span></div><div className="mt-1 text-dim">{usd(profile.starting_equity_usd)} purse · {usd(profile.fixed_margin_usd)} margin · ≤{profile.max_leverage ?? "—"}x · cap {usd(cap)}</div><div className="mt-2 h-1 bg-line/60"><div className="h-full bg-info/80" style={{ width: `${configuredPct}%` }} /></div></div>;
          })}
          {!data.sizing_profiles.length && <div className="text-[11px] text-dim">Sizing telemetry not reported.</div>}
        </div>
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

type BacktestTab = "overview" | "trades" | "days" | "months" | "runs";

const compactDate = (value: string | null | undefined) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "2-digit", timeZone: "UTC" })
    : value;
};

function BacktestEquityChart({ points }: { points: Array<{ ts: string; equity_usd: number; drawdown_pct: number }> }) {
  const width = 1_000;
  const height = 310;
  const pad = { left: 62, right: 18, top: 20, bottom: 34 };
  const equityBottom = 205;
  const drawdownTop = 235;
  const drawdownBottom = 280;
  if (points.length < 2) {
    return <div className="grid h-[310px] place-items-center font-mono text-[11px] text-faint">Equity curve requires at least two samples.</div>;
  }
  const equities = points.map((point) => point.equity_usd);
  const minEquity = Math.min(...equities);
  const maxEquity = Math.max(...equities);
  const equitySpan = Math.max(1, maxEquity - minEquity);
  const minDrawdown = Math.min(...points.map((point) => point.drawdown_pct), -0.01);
  const x = (index: number) => pad.left + index / (points.length - 1) * (width - pad.left - pad.right);
  const y = (value: number) => equityBottom - (value - minEquity) / equitySpan * (equityBottom - pad.top);
  const ddY = (value: number) => drawdownTop + value / minDrawdown * (drawdownBottom - drawdownTop);
  const line = points.map((point, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(point.equity_usd).toFixed(1)}`).join(" ");
  const area = `${line} L${x(points.length - 1).toFixed(1)},${equityBottom} L${x(0).toFixed(1)},${equityBottom} Z`;
  const drawdown = points.map((point, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${ddY(point.drawdown_pct).toFixed(1)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-[310px] w-full" role="img" aria-label="Backtest equity and drawdown curve">
      <defs>
        <linearGradient id="backtest-equity-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#3FB950" stopOpacity=".26" /><stop offset="1" stopColor="#3FB950" stopOpacity=".015" /></linearGradient>
      </defs>
      {ticks.map((tick) => {
        const tickY = pad.top + tick * (equityBottom - pad.top);
        const value = maxEquity - tick * equitySpan;
        return <g key={tick}><line x1={pad.left} x2={width - pad.right} y1={tickY} y2={tickY} stroke="#30363D" strokeWidth="1" /><text x={pad.left - 9} y={tickY + 4} textAnchor="end" fill="#6E7681" fontSize="11" fontFamily="monospace">${value.toFixed(0)}</text></g>;
      })}
      <path d={area} fill="url(#backtest-equity-fill)" />
      <path d={line} fill="none" stroke="#3FB950" strokeWidth="2.2" vectorEffect="non-scaling-stroke" />
      <line x1={pad.left} x2={width - pad.right} y1={drawdownTop} y2={drawdownTop} stroke="#30363D" />
      <line x1={pad.left} x2={width - pad.right} y1={drawdownBottom} y2={drawdownBottom} stroke="#30363D" />
      <path d={drawdown} fill="none" stroke="#F85149" strokeWidth="1.7" vectorEffect="non-scaling-stroke" />
      <text x={pad.left - 9} y={drawdownTop + 4} textAnchor="end" fill="#6E7681" fontSize="10" fontFamily="monospace">0%</text>
      <text x={pad.left - 9} y={drawdownBottom + 4} textAnchor="end" fill="#F85149" fontSize="10" fontFamily="monospace">{minDrawdown.toFixed(1)}%</text>
      <text x={pad.left} y={height - 8} fill="#6E7681" fontSize="10" fontFamily="monospace">{compactDate(points[0]?.ts)}</text>
      <text x={width - pad.right} y={height - 8} textAnchor="end" fill="#6E7681" fontSize="10" fontFamily="monospace">{compactDate(points[points.length - 1]?.ts)}</text>
    </svg>
  );
}

function BacktestLabPanel() {
  const identity = useWhoAmI();
  const [selectedRunId, setSelectedRunId] = useState("");
  const [compareRunId, setCompareRunId] = useState("");
  const [tab, setTab] = useState<BacktestTab>("overview");
  const [submitting, setSubmitting] = useState(false);
  const [submissionMessage, setSubmissionMessage] = useState("");
  const [draft, setDraft] = useState({
    strategy_id: "",
    exchange: "binanceusdm",
    symbol: "BTC/USDT:USDT",
    timeframe: "1h",
    start: "",
    end: "",
    initial_capital_usd: "1000",
    commission_bps: "5",
    slippage_bps: "1",
    max_holding_bars: "48",
  });
  const lab = useBacktestLab(selectedRunId || undefined);
  const comparison = useBacktestLab(compareRunId || undefined);
  const report = lab.data?.selected;
  const overview = report?.overview;
  const activeRunId = selectedRunId || lab.data?.selected_run_id || "";
  const compareReport = compareRunId ? comparison.data?.selected : null;
  const runs = lab.data?.runs ?? [];
  const canQueue = identity.data?.permissions.includes("request_backtest") ?? false;
  const selectedStrategy = draft.strategy_id || lab.data?.catalog.strategies[0] || "";

  const queueRun = async () => {
    if (!selectedStrategy) {
      setSubmissionMessage("No registered strategy is available.");
      return;
    }
    if (draft.start && draft.end && draft.start > draft.end) {
      setSubmissionMessage("The start date must not be after the end date.");
      return;
    }
    if (!(Number(draft.initial_capital_usd) > 0) || !(Number(draft.max_holding_bars) >= 1)) {
      setSubmissionMessage("Start equity and max-hold bars must be positive.");
      return;
    }
    setSubmitting(true);
    setSubmissionMessage("");
    try {
      const job = await apiPost<BacktestJobAccepted>("/backtest-lab/runs", {
        strategy_id: selectedStrategy,
        exchange: draft.exchange,
        symbol: draft.symbol,
        timeframe: draft.timeframe,
        start: draft.start || null,
        end: draft.end || null,
        initial_capital_usd: Number(draft.initial_capital_usd),
        commission_bps: draft.commission_bps === "" ? null : Number(draft.commission_bps),
        slippage_bps: draft.slippage_bps === "" ? null : Number(draft.slippage_bps),
        strict_mode: true,
        live_orders_enabled: false,
        parameters: { max_holding_bars: Number(draft.max_holding_bars) },
        hypothesis_id: null,
        notes: "Queued from the VNEDGE Backtest Lab",
      });
      setSelectedRunId(job.job_id);
      setTab("runs");
      setSubmissionMessage(`${job.job_id} queued for the bounded research worker.`);
      await lab.refetch();
    } catch (error) {
      setSubmissionMessage(error instanceof Error ? error.message : "Backtest request failed.");
    } finally {
      setSubmitting(false);
    }
  };

  const exportReport = () => {
    if (!report) return;
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${report.run.run_id}.backtest.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const metric = (label: string, value: string, tone = "", sub?: string) => (
    <div className="min-h-[84px] border border-line bg-inset p-3">
      <div className="font-mono text-[9px] uppercase tracking-wider text-faint">{label}</div>
      <div className={`mt-1 font-mono text-xl font-black tabular-nums ${tone}`}>{value}</div>
      {sub && <div className="mt-1 truncate text-[9px] text-faint" title={sub}>{sub}</div>}
    </div>
  );
  const comparisonDelta = compareReport && overview ? {
    net: overview.net_profit_usd - compareReport.overview.net_profit_usd,
    trades: overview.num_trades - compareReport.overview.num_trades,
    sharpe: overview.sharpe - compareReport.overview.sharpe,
    drawdown: overview.max_drawdown_pct - compareReport.overview.max_drawdown_pct,
  } : null;

  const tradeCols: Column<BacktestTrade>[] = [
    { key: "entry", header: "Entry UTC", render: (row) => <span className="whitespace-nowrap font-mono">{new Date(row.entry_ts).toLocaleString("en-GB", { timeZone: "UTC", hour12: false })}</span> },
    { key: "side", header: "Side", render: (row) => <TerminalBadge tone={row.side.toLowerCase() === "long" ? "good" : "bad"}>{row.side}</TerminalBadge> },
    { key: "price", header: "Entry → exit", align: "right", render: (row) => <span className="whitespace-nowrap font-mono">{priceText(row.entry_price)} → {priceText(row.exit_price)}</span> },
    { key: "hold", header: "Hold", align: "right", render: (row) => ageSec(row.hold_seconds) },
    { key: "gross", header: "Gross", align: "right", render: (row) => <span className={signed(row.gross_pnl_usd)}>{usd(row.gross_pnl_usd)}</span> },
    { key: "cost", header: "Fees + funding", align: "right", render: (row) => usd(row.fees_usd - row.funding_usd) },
    { key: "net", header: "Net", align: "right", render: (row) => <span className={signed(row.net_pnl_usd)}>{usd(row.net_pnl_usd)}</span> },
    { key: "bps", header: "Net bps", align: "right", render: (row) => row.net_bps_on_entry_notional == null ? "—" : `${row.net_bps_on_entry_notional.toFixed(1)}` },
    { key: "path", header: "MAE / MFE", align: "right", render: (row) => <span className="whitespace-nowrap font-mono">{row.mae_bps_on_entry_notional == null || row.mfe_bps_on_entry_notional == null ? `${usd(row.mae_usd)} / ${usd(row.mfe_usd)}` : `${row.mae_bps_on_entry_notional.toFixed(1)} / +${row.mfe_bps_on_entry_notional.toFixed(1)} bps`}</span> },
    { key: "exit", header: "Exit", render: (row) => <span className="font-mono text-[10px]">{row.exit_reason}</span> },
  ];
  const dayCols: Column<BacktestDay>[] = [
    { key: "date", header: "Day UTC", render: (row) => <span className="font-mono">{row.date}</span> },
    { key: "pnl", header: "Realized net", align: "right", render: (row) => <span className={signed(row.net_pnl_usd)}>{usd(row.net_pnl_usd)}</span> },
    { key: "trades", header: "Trades", align: "right", render: (row) => row.trade_count },
    { key: "wl", header: "W / L", align: "right", render: (row) => `${row.wins} / ${row.losses}` },
    { key: "equity", header: "Close equity", align: "right", render: (row) => usd(row.equity_usd) },
    { key: "change", header: "Equity change", align: "right", render: (row) => <span className={signed(row.equity_change_usd)}>{usd(row.equity_change_usd)}</span> },
    { key: "dd", header: "Drawdown", align: "right", render: (row) => <span className={row.drawdown_pct < 0 ? "text-short" : ""}>{row.drawdown_pct.toFixed(2)}%</span> },
  ];
  const monthCols: Column<BacktestMonth>[] = [
    { key: "month", header: "Month", render: (row) => <span className="font-mono">{row.month}</span> },
    { key: "pnl", header: "Net", align: "right", render: (row) => <span className={signed(row.net_pnl_usd)}>{usd(row.net_pnl_usd)}</span> },
    { key: "days", header: "Traded days", align: "right", render: (row) => row.traded_days },
    { key: "trades", header: "Trades", align: "right", render: (row) => row.trade_count },
    { key: "wl", header: "Win / loss days", align: "right", render: (row) => `${row.win_days} / ${row.loss_days}` },
    { key: "best", header: "Best day", align: "right", render: (row) => <span className="text-long">{usd(row.best_day_usd)}</span> },
    { key: "worst", header: "Worst day", align: "right", render: (row) => <span className="text-short">{usd(row.worst_day_usd)}</span> },
    { key: "dd", header: "Max DD", align: "right", render: (row) => <span className="text-short">{row.max_drawdown_pct.toFixed(2)}%</span> },
  ];
  const runCols: Column<BacktestRunSummary>[] = [
    { key: "run", header: "Run", render: (row) => <button type="button" onClick={() => { setSelectedRunId(row.run_id); setTab("overview"); }} className="max-w-[240px] truncate font-mono text-info hover:underline" title={row.run_id}>{row.run_id}</button> },
    { key: "status", header: "Status", render: (row) => <TerminalBadge tone={row.has_report ? "good" : row.status === "FAILED" || row.status === "BLOCKED" ? "bad" : "warn"}>{row.status}</TerminalBadge> },
    { key: "strategy", header: "Strategy", render: (row) => <span className="font-mono">{row.strategy_id ?? "—"}</span> },
    { key: "market", header: "Market / TF", render: (row) => <span className="whitespace-nowrap font-mono">{row.exchange ?? "—"} · {row.symbol ?? "—"} · {row.timeframe ?? "—"}</span> },
    { key: "net", header: "Net", align: "right", render: (row) => <span className={signed(row.net_profit_usd)}>{usd(row.net_profit_usd)}</span> },
    { key: "trades", header: "Trades", align: "right", render: (row) => row.num_trades ?? "—" },
    { key: "updated", header: "Updated UTC", align: "right", render: (row) => compactDate(row.updated_at) },
    { key: "reason", header: "Reason", render: (row) => <span className="max-w-[260px] truncate text-[10px] text-dim" title={row.error ?? row.blocked_reason ?? ""}>{row.error ?? row.blocked_reason ?? (row.has_report ? "canonical report" : "awaiting worker")}</span> },
  ];

  const monthlyScale = useMemo(() => Math.max(1, ...(report?.monthly ?? []).map((row) => Math.abs(row.net_pnl_usd))), [report?.monthly]);
  const fieldClass = "w-full border border-line bg-bg px-2.5 py-2 font-mono text-[11px] text-txt focus:border-brand focus:outline-none";
  return (
    <TerminalPanel title="Backtest Lab" meta="canonical engine · complete audit trail · research only">
      <details className="mb-3 border border-line bg-inset" open={!report}>
        <summary className="cursor-pointer list-none px-3 py-2 font-mono text-[11px] text-info">New bounded run <span className="float-right text-faint">research worker · strict mode · no orders ▾</span></summary>
        <div className="grid gap-px border-t border-line bg-line p-px sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
          <label className="bg-inset p-2"><span className="mb-1 block font-mono text-[9px] uppercase text-faint">Strategy</span><select value={selectedStrategy} onChange={(event) => setDraft({ ...draft, strategy_id: event.target.value })} className={fieldClass}>{(lab.data?.catalog.strategies ?? []).map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <label className="bg-inset p-2"><span className="mb-1 block font-mono text-[9px] uppercase text-faint">Exchange</span><select value={draft.exchange} onChange={(event) => setDraft({ ...draft, exchange: event.target.value })} className={fieldClass}>{(lab.data?.catalog.exchanges ?? [draft.exchange]).map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <label className="bg-inset p-2"><span className="mb-1 block font-mono text-[9px] uppercase text-faint">Market</span><select value={draft.symbol} onChange={(event) => setDraft({ ...draft, symbol: event.target.value })} className={fieldClass}>{(lab.data?.catalog.symbols ?? [draft.symbol]).map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <label className="bg-inset p-2"><span className="mb-1 block font-mono text-[9px] uppercase text-faint">Timeframe</span><select value={draft.timeframe} onChange={(event) => setDraft({ ...draft, timeframe: event.target.value })} className={fieldClass}>{(lab.data?.catalog.timeframes ?? [draft.timeframe]).map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <label className="bg-inset p-2"><span className="mb-1 block font-mono text-[9px] uppercase text-faint">From UTC</span><input type="date" value={draft.start} onChange={(event) => setDraft({ ...draft, start: event.target.value })} className={fieldClass} /></label>
          <label className="bg-inset p-2"><span className="mb-1 block font-mono text-[9px] uppercase text-faint">To UTC</span><input type="date" value={draft.end} onChange={(event) => setDraft({ ...draft, end: event.target.value })} className={fieldClass} /></label>
          <label className="bg-inset p-2"><span className="mb-1 block font-mono text-[9px] uppercase text-faint">Start equity USD</span><input type="number" min="1" max="1000000" value={draft.initial_capital_usd} onChange={(event) => setDraft({ ...draft, initial_capital_usd: event.target.value })} className={fieldClass} /></label>
          <label className="bg-inset p-2"><span className="mb-1 block font-mono text-[9px] uppercase text-faint">Fee / leg bps</span><input type="number" min="0" max="100" step="0.1" value={draft.commission_bps} onChange={(event) => setDraft({ ...draft, commission_bps: event.target.value })} className={fieldClass} /></label>
          <label className="bg-inset p-2"><span className="mb-1 block font-mono text-[9px] uppercase text-faint">Slip / leg bps</span><input type="number" min="0" max="100" step="0.1" value={draft.slippage_bps} onChange={(event) => setDraft({ ...draft, slippage_bps: event.target.value })} className={fieldClass} /></label>
          <label className="bg-inset p-2"><span className="mb-1 block font-mono text-[9px] uppercase text-faint">Max hold bars</span><input type="number" min="1" max="10000" value={draft.max_holding_bars} onChange={(event) => setDraft({ ...draft, max_holding_bars: event.target.value })} className={fieldClass} /></label>
          <div className="flex items-end bg-inset p-2 sm:col-span-2"><button type="button" onClick={() => void queueRun()} disabled={submitting || !selectedStrategy || !canQueue} className="w-full border border-brand/50 bg-brand/10 px-3 py-2 font-mono text-[11px] font-bold uppercase text-brand hover:bg-brand/20 disabled:cursor-not-allowed disabled:opacity-40">{submitting ? "Queueing…" : canQueue ? "Run backtest" : "Operator permission required"}</button></div>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-3 py-2 text-[10px] text-faint"><span>Close-decision → next-open fills · sizing/risk reused · costs and funding explicit</span><span className={submissionMessage.toLowerCase().includes("failed") || submissionMessage.toLowerCase().includes("required") ? "text-short" : "text-info"}>{submissionMessage || "Operator-only queue; execution remains outside the dashboard process."}</span></div>
      </details>
      <div className="flex flex-wrap items-end gap-3 border-b border-line pb-3">
        <label className="min-w-[280px] flex-1"><span className="mb-1 block font-mono text-[9px] uppercase tracking-wider text-faint">Selected run</span><select aria-label="Selected backtest run" value={activeRunId} onChange={(event) => setSelectedRunId(event.target.value)} className="w-full border border-line bg-inset px-2.5 py-2 font-mono text-[11px] text-txt focus:border-brand focus:outline-none"><option value="">latest completed report</option>{runs.map((row) => <option key={row.run_id} value={row.run_id}>{row.status} · {row.strategy_id ?? "unknown"} · {row.symbol ?? "—"} {row.timeframe ?? "—"} · {row.run_id}</option>)}</select></label>
        <label className="min-w-[220px]"><span className="mb-1 block font-mono text-[9px] uppercase tracking-wider text-faint">Compare to</span><select aria-label="Compare backtest run" value={compareRunId} onChange={(event) => setCompareRunId(event.target.value)} className="w-full border border-line bg-inset px-2.5 py-2 font-mono text-[11px] text-txt focus:border-brand focus:outline-none"><option value="">none</option>{runs.filter((row) => row.has_report && row.run_id !== activeRunId).map((row) => <option key={row.run_id} value={row.run_id}>{row.strategy_id ?? "unknown"} · {row.symbol ?? "—"} · {row.run_id}</option>)}</select></label>
        <button type="button" onClick={exportReport} disabled={!report} className="border border-line px-3 py-2 font-mono text-[11px] text-dim hover:border-brand hover:text-brand disabled:cursor-not-allowed disabled:opacity-40">Export JSON</button>
        <TerminalBadge tone={report?.run.evidence_class === "SEALED_OOS" ? "good" : "warn"}>{report?.run.evidence_class ?? "no report"}</TerminalBadge>
      </div>

      {lab.isError && <div className="mt-3 border border-short/40 bg-short/5 px-3 py-3 text-[11px] text-short" role="alert">Backtest catalog unavailable. No result is being asserted.</div>}
      {!lab.isLoading && !report && <div className="mt-3 rounded-md border border-warn/40 bg-warn/5 p-4"><div className="font-semibold text-warn">No canonical backtest report yet.</div><div className="mt-1 text-[11px] text-dim">Queue a bounded Agent Gateway backtest, then run the isolated worker. The dashboard never executes research inline.</div><code className="mt-3 block overflow-x-auto border border-line bg-bg px-3 py-2 font-mono text-[10px] text-info">{lab.data?.submission.worker_command ?? "python -m vnedge.agent_gateway.job_runner --once --json"}</code></div>}

      {report && overview && <>
        <div className="mt-3 grid grid-cols-2 gap-px border border-line bg-line md:grid-cols-4 xl:grid-cols-8">
          <div className="bg-inset p-2.5"><div className="text-[9px] font-mono text-faint">STRATEGY</div><div className="mt-1 truncate font-mono text-[11px]" title={report.run.strategy_id}>{report.run.strategy_id}</div></div>
          <div className="bg-inset p-2.5"><div className="text-[9px] font-mono text-faint">MARKET</div><div className="mt-1 truncate font-mono text-[11px]">{report.run.exchange} · {report.run.symbol}</div></div>
          <div className="bg-inset p-2.5"><div className="text-[9px] font-mono text-faint">TIMEFRAME / BARS</div><div className="mt-1 font-mono text-[11px]">{report.run.timeframe} · {report.run.bars.toLocaleString("en-US")}</div></div>
          <div className="bg-inset p-2.5"><div className="text-[9px] font-mono text-faint">WINDOW UTC</div><div className="mt-1 font-mono text-[11px]">{compactDate(report.run.window.start)} → {compactDate(report.run.window.end)}</div></div>
          <div className="bg-inset p-2.5"><div className="text-[9px] font-mono text-faint">START EQUITY</div><div className="mt-1 font-mono text-[11px]">{usd(report.run.initial_equity_usd)}</div></div>
          <div className="bg-inset p-2.5"><div className="text-[9px] font-mono text-faint">BOOKED / GATE RT</div><div className="mt-1 font-mono text-[11px] text-warn">{(report.run.costs.execution_round_trip_bps ?? report.run.costs.modeled_taker_round_trip_bps).toFixed(1)} / {(report.run.costs.gate_round_trip_bps ?? report.run.costs.modeled_taker_round_trip_bps).toFixed(1)} bps</div></div>
          <div className="bg-inset p-2.5"><div className="text-[9px] font-mono text-faint">FUNDING</div><div className="mt-1 font-mono text-[11px]">{report.run.costs.funding_included ? `included · ${report.run.costs.funding_event_count ?? "?"}` : "excluded"}</div></div>
          <div className="bg-inset p-2.5"><div className="text-[9px] font-mono text-faint">ENGINE</div><div className="mt-1 truncate font-mono text-[11px]" title={report.run.engine}>{report.run.engine}</div></div>
        </div>

        <div className="my-3 flex flex-wrap gap-1 border-b border-line pb-2">
          {(["overview", "trades", "days", "months", "runs"] as BacktestTab[]).map((name) => <button key={name} type="button" onClick={() => setTab(name)} className={`border px-3 py-1.5 font-mono text-[11px] uppercase ${tab === name ? "border-brand/50 bg-brand/10 text-brand" : "border-transparent text-dim hover:text-txt"}`}>{name}{name === "trades" ? ` ${report.trades.length}` : name === "runs" ? ` ${runs.length}` : ""}</button>)}
        </div>

        {comparisonDelta && <div className="mb-3 flex flex-wrap items-center gap-4 border-l-2 border-info bg-info/5 px-3 py-2 font-mono text-[10px]"><span className="text-info">VS {compareReport?.run.run_id}</span><span>net <b className={signed(comparisonDelta.net)}>{usd(comparisonDelta.net)}</b></span><span>trades {comparisonDelta.trades > 0 ? "+" : ""}{comparisonDelta.trades}</span><span>Sharpe {comparisonDelta.sharpe > 0 ? "+" : ""}{comparisonDelta.sharpe.toFixed(2)}</span><span>DD {comparisonDelta.drawdown > 0 ? "+" : ""}{comparisonDelta.drawdown.toFixed(2)}pp</span></div>}

        {tab === "overview" && <div className="space-y-3">
          <div className="grid grid-cols-2 gap-px border border-line bg-line md:grid-cols-4 xl:grid-cols-7">
            {metric("Gross P&L", usd(overview.gross_profit_usd), signed(overview.gross_profit_usd), "before costs")}
            {metric("Net P&L", usd(overview.net_profit_usd), signed(overview.net_profit_usd), `${usd(overview.total_cost_usd)} modeled cost`)}
            {metric("Return", `${overview.return_pct >= 0 ? "+" : ""}${overview.return_pct.toFixed(2)}%`, signed(overview.return_pct), "on starting equity")}
            {metric("Sharpe", overview.sharpe.toFixed(2), overview.sharpe > 1 ? "text-long" : overview.sharpe < 0 ? "text-short" : "", "annualized bar returns")}
            {metric("Profit factor", overview.profit_factor == null ? "undefined" : overview.profit_factor.toFixed(2), overview.profit_factor != null && overview.profit_factor >= 1.25 ? "text-long" : "text-warn", overview.num_trades < 30 ? "under-sampled" : "after cost")}
            {metric("Max drawdown", `-${overview.max_drawdown_pct.toFixed(2)}%`, "text-short", `${overview.longest_underwater_days.toFixed(1)} days underwater`)}
            {metric("Win rate", `${overview.win_rate_pct.toFixed(1)}%`, "", `${overview.num_trades} closed trades`)}
          </div>
          <div className="grid gap-3 xl:grid-cols-[minmax(0,1.55fr)_minmax(340px,.75fr)]">
            <div className="border border-line bg-inset"><div className="flex items-center justify-between border-b border-line px-3 py-2"><div className="font-mono text-[10px] uppercase text-faint">Equity + drawdown</div><div className={`font-mono text-[12px] font-bold ${signed(overview.net_profit_usd)}`}>{usd(report.run.initial_equity_usd + overview.net_profit_usd)}</div></div><BacktestEquityChart points={report.equity_curve} /></div>
            <div className="grid grid-cols-2 gap-px border border-line bg-line">
              {metric("Traded days", String(overview.traded_days), "", `${overview.win_days} win · ${overview.loss_days} loss`)}
              {metric("Average day", usd(overview.avg_day_pnl_usd), signed(overview.avg_day_pnl_usd))}
              {metric("Best day", usd(overview.best_day_usd), "text-long")}
              {metric("Worst day", usd(overview.worst_day_usd), "text-short")}
              {metric("Avg win", usd(overview.avg_win_usd), "text-long")}
              {metric("Avg loss", usd(overview.avg_loss_usd), "text-short")}
              {metric("Payoff", `1 : ${overview.payoff_ratio.toFixed(2)}`, "", "average win / average loss")}
              {metric("Avg hold", `${overview.avg_hold_hours.toFixed(1)}h`, "", `max ${report.run.exit_contract.max_holding_bars} bars`)}
              {metric("Win streak", String(overview.max_win_streak), "text-long")}
              {metric("Loss streak", String(overview.max_loss_streak), "text-short")}
              {metric("Calmar", overview.calmar == null ? "—" : overview.calmar.toFixed(2))}
              {metric("Best-trade share", overview.best_trade_profit_share_pct == null ? "—" : `${overview.best_trade_profit_share_pct.toFixed(1)}%`, overview.best_trade_profit_share_pct != null && overview.best_trade_profit_share_pct > 35 ? "text-warn" : "")}
            </div>
          </div>
          <div className="border border-line bg-inset p-3"><div className="font-mono text-[10px] uppercase text-faint">Monthly attribution</div><div className="mt-3 flex h-36 items-center gap-2 overflow-x-auto border-b border-line px-2">{report.monthly.map((row) => <div key={row.month} className="flex h-full min-w-[58px] flex-col items-center justify-end"><div title={`${row.month}: ${usd(row.net_pnl_usd)}`} className={`w-8 ${row.net_pnl_usd >= 0 ? "bg-long/70" : "bg-short/70"}`} style={{ height: `${Math.max(3, Math.abs(row.net_pnl_usd) / monthlyScale * 100)}px` }} /><span className={`mt-1 font-mono text-[9px] ${signed(row.net_pnl_usd)}`}>{row.month.slice(5)}</span></div>)}{!report.monthly.length && <div className="m-auto font-mono text-[11px] text-faint">No monthly attribution.</div>}</div></div>
          {!!report.warnings.length && <div className="border-l-2 border-warn bg-warn/5 px-3 py-2"><div className="font-mono text-[9px] uppercase text-warn">Evidence warnings</div>{report.warnings.map((warning) => <div key={warning} className="mt-1 text-[10px] text-dim">• {warning}</div>)}</div>}
        </div>}
        {tab === "trades" && <DenseTable columns={tradeCols} rows={report.trades} rowKey={(row) => `${row.entry_ts}:${row.side}`} empty="No closed trades in this run." />}
        {tab === "days" && <DenseTable columns={dayCols} rows={report.daily} rowKey={(row) => row.date} empty="No daily equity samples." />}
        {tab === "months" && <DenseTable columns={monthCols} rows={report.monthly} rowKey={(row) => row.month} empty="No monthly attribution." />}
        {tab === "runs" && <DenseTable columns={runCols} rows={runs} rowKey={(row) => row.run_id} empty="No bounded backtest jobs recorded." />}
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3 text-[10px] text-faint"><span>Generated {new Date(report.run.generated_at).toLocaleString("en-GB", { timeZone: "UTC", hour12: false })} UTC · source {report.run.data_source}</span><span>Read only · cannot trade · cannot promote</span></div>
      </>}
    </TerminalPanel>
  );
}

function StrategyWorkflowPanel() {
  const workflow = useStrategyWorkflow();
  const [stage, setStage] = useState("all");
  const [symbol, setSymbol] = useState("all");
  const [minTrades, setMinTrades] = useState(0);
  const revisions = workflow.data?.revisions ?? [];
  const stages = Array.from(new Set(revisions.map((row) => row.stage))).sort();
  const symbols = Array.from(new Set(revisions.flatMap((row) => [
    ...row.symbols,
    row.latest_run?.symbol ?? "",
  ]).filter(Boolean))).sort();
  const filtered = revisions.filter((row) => {
    const rowSymbols = [...row.symbols, row.latest_run?.symbol ?? ""].filter(Boolean);
    const trades = row.performance.trades;
    return (stage === "all" || row.stage === stage)
      && (symbol === "all" || rowSymbols.includes(symbol))
      && (minTrades <= 0 || (trades != null && trades >= minTrades));
  });
  const stageTone = (value: string) => {
    if (["OOS_PASS", "SHADOW_OBSERVE"].includes(value)) return "good";
    if (["BACKTESTED", "PREREGISTERED"].includes(value)) return "info";
    if (["QUARANTINED", "KILLED", "OOS_REJECT"].includes(value)) return "bad";
    return "neutral";
  };
  const cols: Column<(typeof filtered)[number]>[] = [
    {
      key: "revision",
      header: "Revision / lineage",
      render: (row) => (
        <span className="block min-w-[220px]">
          <span className="font-mono text-txt">{row.strategy_id} · v{row.version}</span>
          <span className="block max-w-[260px] truncate text-[9px] text-faint" title={row.revision_id}>
            {row.parent_revision_id ? `fork ← ${row.parent_revision_id.split("+")[0]}` : "root revision"}
          </span>
        </span>
      ),
    },
    { key: "stage", header: "Stage", render: (row) => <TerminalBadge tone={stageTone(row.stage)}>{row.stage.replace(/_/g, " ")}</TerminalBadge> },
    {
      key: "net",
      header: "After cost",
      align: "right",
      render: (row) => <span className={signed(row.performance.after_cost_net_usd)}>{usd(row.performance.after_cost_net_usd)}</span>,
    },
    { key: "trades", header: "Trades", align: "right", render: (row) => <span className={row.performance.sample_qualified ? "" : "text-warn"}>{row.performance.trades ?? "—"}</span> },
    { key: "pf", header: "PF", align: "right", render: (row) => row.performance.profit_factor?.toFixed(2) ?? "—" },
    {
      key: "parity",
      header: "Engine parity",
      render: (row) => (
        <span className="block min-w-[130px]">
          <TerminalBadge tone={row.parity_status === "PASS" ? "good" : row.parity_status === "FAIL" ? "bad" : "warn"}>{row.parity_status.replace(/_/g, " ")}</TerminalBadge>
          <span className="mt-1 block text-[9px] text-faint">{[row.backtest_engine, row.engine_version].filter(Boolean).join(" · ") || "engine not frozen"}</span>
        </span>
      ),
    },
    {
      key: "governance",
      header: "Governance",
      render: (row) => (
        <span className="block min-w-[180px]">
          <span className={row.governance_flags.length ? "text-warn" : "text-long"}>{row.governance_flags[0]?.replace(/_/g, " ") ?? "contracts complete"}</span>
          <span className="block max-w-[230px] truncate text-[9px] text-faint" title={row.governance_flags.join(" · ")}>{row.preregistration ? "pre-registration linked" : "no pre-registration linked"} · read only</span>
        </span>
      ),
    },
  ];
  const summary = workflow.data?.summary;
  const metric = (label: string, value: number | undefined, tone = "") => (
    <div className="border border-line bg-inset p-3">
      <div className="font-mono text-[9px] uppercase tracking-wider text-faint">{label}</div>
      <div className={`mt-1 font-mono text-xl font-black ${tone}`}>{value ?? 0}</div>
    </div>
  );
  return (
    <TerminalPanel title="Strategy Workflow" meta="immutable versions · lineage · OOS · quarantine">
      {workflow.isError && <div className="mb-3 border border-short/40 bg-short/5 px-3 py-2 text-[11px] text-short" role="alert">Strategy workflow unavailable. No revision or parity state is being asserted.</div>}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        {metric("Revisions", summary?.revisions)}
        {metric("Explicit", summary?.explicit_revisions, "text-brand")}
        {metric("OOS pass", summary?.oos_pass, "text-long")}
        {metric("Shadow observe", summary?.shadow_observe, "text-info")}
        {metric("Quarantined", summary?.quarantined, (summary?.quarantined ?? 0) > 0 ? "text-short" : "")}
      </div>
      <div className="my-3 flex flex-wrap items-center gap-2">
        <select aria-label="Filter workflow stage" value={stage} onChange={(event) => setStage(event.target.value)} className="border border-line bg-inset px-2.5 py-1.5 font-mono text-[10px] text-dim focus:border-brand focus:outline-none">
          <option value="all">all stages</option>{stages.map((value) => <option key={value} value={value}>{value.replace(/_/g, " ").toLowerCase()}</option>)}
        </select>
        <select aria-label="Filter workflow symbol" value={symbol} onChange={(event) => setSymbol(event.target.value)} className="border border-line bg-inset px-2.5 py-1.5 font-mono text-[10px] text-dim focus:border-brand focus:outline-none">
          <option value="all">all symbols</option>{symbols.map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
        <input aria-label="Minimum workflow trades" type="number" min={0} value={minTrades || ""} onChange={(event) => setMinTrades(Math.max(0, Number(event.target.value) || 0))} placeholder="min trades" className="w-24 border border-line bg-inset px-2.5 py-1.5 font-mono text-[10px] text-txt placeholder:text-faint focus:border-brand focus:outline-none" />
        <span className="ml-auto text-[10px] font-mono text-faint">{filtered.length}/{revisions.length} shown · after-cost evidence · no fork/promote controls</span>
      </div>
      <DenseTable columns={cols} rows={filtered} rowKey={(row) => row.revision_id} empty={workflow.isLoading ? "loading immutable workflow…" : "no revisions match these filters"} />
      <div className="mt-3 border-l-2 border-warn px-3 py-2 text-[10px] text-dim"><strong className="text-txt">Boundary:</strong> forks require a new reviewed strategy ID. OOS PASS still cannot trade or promote from this screen.</div>
    </TerminalPanel>
  );
}

export function ResearchPanel() {
  const scorecard = useResearchScorecard();
  const risk = useRiskSnapshot();
  const lanes = useLanes();
  const scoreFreshness = artifactFreshness("research scorecard", scorecard.data?.artifact);
  const [showUndersampled, setShowUndersampled] = useState(false);
  const artifacts = [
    ["Strategy scorecard", "/scorecard"],
    ["OOS research evidence", "/research"],
    ["Pre-live checklist", "/pre-live-checklist"],
    ["Promotion review runbook", "/promotion-review-runbook"],
  ];
  const scoreRows = (scorecard.data?.strategies ?? []).filter((row) => showUndersampled || row.sample_qualified);
  const runtimeAlignment = scorecard.data?.runtime_alignment ?? [];
  const scoreCols: Column<(typeof scoreRows)[number]>[] = [
    { key: "strategy", header: "Strategy", render: (r) => <span className="font-mono">{r.strategy}</span> },
    { key: "samples", header: "n", align: "right", render: (r) => <span className={r.sample_qualified ? "" : "text-warn"} title={`${r.sample_unit}; ${r.samples_total} across all cells`}>{r.samples}</span> },
    { key: "net", header: "OOS net", align: "right", render: (r) => r.oos_net_bps == null ? "—" : `${r.oos_net_bps.toFixed(2)} bps` },
    { key: "pf", header: "PF · net", align: "right", render: (r) => <span title={r.profit_factor_reason ?? "after fees"}>{r.profit_factor_display}</span> },
    { key: "sharpe", header: "Sharpe", align: "right", render: (r) => <span title={r.sharpe_reason ?? r.sharpe_convention}>{r.metric_state === "UNDER_SAMPLED" ? "hidden" : r.sharpe == null ? "—" : r.sharpe.toFixed(2)}</span> },
    { key: "dsr", header: "DSR", align: "right", render: (r) => r.metric_state === "UNDER_SAMPLED" ? "hidden" : r.deflated_sharpe == null ? "—" : <span className={r.deflated_sharpe_pass ? "text-long" : "text-short"}>{r.deflated_sharpe.toFixed(3)} · {r.deflated_sharpe_pass ? "PASS" : "FAIL"}</span> },
    { key: "trials", header: "N / N_eff", align: "right", render: (r) => <span title={r.trial_count_reason ?? "raw / correlation-adjusted trials"}>{r.raw_trials == null || r.effective_trials == null ? "not reported" : `${r.raw_trials.toFixed(0)} / ${r.effective_trials.toFixed(1)}`}</span> },
    { key: "dd", header: "Max DD", align: "right", render: (r) => r.max_drawdown_pct == null ? "—" : `${r.max_drawdown_pct.toFixed(2)}%` },
    { key: "verdict", header: "Evidence", render: (r) => <TerminalBadge tone={r.metric_state === "UNDER_SAMPLED" ? "warn" : String(r.verdict).toLowerCase().includes("pass") ? "info" : "neutral"}>{r.metric_state === "UNDER_SAMPLED" ? "UNDER_SAMPLED" : r.verdict ?? "unreported"}</TerminalBadge> },
  ];
  const alignmentCols: Column<(typeof runtimeAlignment)[number]>[] = [
    { key: "strategy", header: "Current scanner", render: (row) => <span className="font-mono">{row.strategy_id}</span> },
    { key: "lanes", header: "Lanes", align: "right", render: (row) => row.lane_count },
    { key: "markets", header: "Markets / TF", render: (row) => <span className="font-mono text-[10px]">{row.symbols.join(", ")} · {row.timeframes.join(", ")}</span> },
    { key: "outcomes", header: "Resolved / pending", align: "right", render: (row) => `${row.resolved_outcomes} / ${row.pending_intents}` },
    { key: "evidence", header: "Evidence link", render: (row) => <TerminalBadge tone={row.status === "EVIDENCE_MATCH" ? "good" : row.status === "RUNTIME_OUTCOMES_NOT_SCORED" ? "warn" : "neutral"}>{row.status.replace(/_/g, " ")}</TerminalBadge> },
  ];
  return (
    <div className="space-y-4">
      <BacktestLabPanel />
      <StrategyWorkflowPanel />
      <TerminalPanel title="Research" meta="evidence only · no mutation">
      <div className="mb-4 rounded-lg border border-line bg-inset px-3 py-2 text-[11px] text-dim"><strong className="text-txt">Scorecard {scoreFreshness.state.toLowerCase()}.</strong> Evidence as of {scoreFreshness.age}; historical age is provenance, not a runtime outage.</div>
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
          <div><div className="font-mono text-[10px] uppercase tracking-wider text-faint">After-cost OOS scorecard</div><div className="mt-1 text-[11px] text-dim">PF, Sharpe, and DSR use one evidence cell and require n ≥ {scorecard.data?.performance_policy.min_samples ?? 30}. Sharpe is hidden unless its annualization convention is declared.</div></div>
          <label className="flex items-center gap-2 text-[11px] text-dim"><input type="checkbox" checked={showUndersampled} onChange={(event) => setShowUndersampled(event.target.checked)} /> show undersampled</label>
        </div>
        <DenseTable columns={scoreCols} rows={scoreRows} empty={scorecard.isLoading ? "loading evidence…" : "no sample-qualified scorecard rows"} />
      </div>
      <div className="mt-4 rounded-lg border border-line bg-inset p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div><div className="font-mono text-[10px] uppercase tracking-wider text-faint">Current runtime ↔ evidence</div><div className="mt-1 text-[11px] text-dim">Exact strategy IDs only. A missing match is shown explicitly and never inherits evidence from an older scanner version.</div></div>
          <TerminalBadge tone={runtimeAlignment.every((row) => row.scorecard_match) && runtimeAlignment.length ? "good" : "warn"}>{runtimeAlignment.filter((row) => row.scorecard_match).length}/{runtimeAlignment.length} matched</TerminalBadge>
        </div>
        <DenseTable columns={alignmentCols} rows={runtimeAlignment} empty={scorecard.isLoading ? "loading runtime alignment…" : "no active shadow scanners"} />
      </div>
      <div className="mt-4 rounded-lg border border-short/30 bg-short/5 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div><div className="font-mono text-[10px] uppercase tracking-wider text-faint">Promotion gate</div><div className="mt-1 text-[11px] text-dim">Human-reviewed evidence gate only; this UI has no promotion mutation.</div></div>
          <div className="flex flex-wrap gap-2">
            <TerminalBadge tone="neutral">capital roster {risk.data?.capital.roster_size ?? 0}</TerminalBadge>
            <TerminalBadge tone="bad">live checklist {risk.data?.live_checklist.passed ?? 0}/{risk.data?.live_checklist.total ?? 7}</TerminalBadge>
            <TerminalBadge tone="warn">qualified {(scorecard.data?.strategies ?? []).filter((row) => row.sample_qualified).length}</TerminalBadge>
            <TerminalBadge tone="neutral">runtime scanners {(lanes.data?.lanes ?? []).filter((row) => row.observation_class === "shadow_observe").length}</TerminalBadge>
          </div>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        {artifacts.map(([label, path]) => (
          <a key={path} href={path} target="_blank" rel="noreferrer" className="flex items-center justify-between rounded-lg border border-line bg-inset px-4 py-3 text-[12px] hover:border-line2">
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
  const mlAvailable = !ml.isError && ml.data?.artifact_available !== false;
  const agentAvailable = !agents.isError && agents.data?.artifact_available !== false;
  const dataset = mlAvailable ? ml.data?.dataset : undefined;
  const summary = agentAvailable ? agents.data?.summary : undefined;
  const gateRows = mlAvailable ? Object.entries(ml.data?.gates ?? {}).slice(0, 6) : [];
  const locked = mlAvailable && !ml.data?.can_promote && !ml.data?.can_trade;
  const mlFreshness = artifactFreshness("ML pipeline", ml.data?.artifact);
  const agentFreshness = artifactFreshness("agent governor", agents.data?.artifact);
  const gateValue = (value: unknown) => {
    if (typeof value === "number") return value.toLocaleString("en-US");
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
            <TerminalBadge tone={!mlAvailable ? "bad" : mlFreshness.state !== "OK" ? "warn" : locked ? "warn" : "bad"}>{!mlAvailable ? "status unavailable" : mlFreshness.state !== "OK" ? `${mlFreshness.state.toLowerCase()} · ${mlFreshness.age}` : locked ? "gates locked" : "authority mismatch"}</TerminalBadge>
          </div>
          {!mlAvailable && <div className="mt-4 rounded-md border border-short/40 bg-short/5 px-3 py-2 text-[11px] text-short" role="alert">ML status artifact unavailable. No stage, label count, or gate state is being asserted.</div>}
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div><div className="text-[10px] font-mono text-faint">STAGE</div><div className="mt-1 text-[12px] font-mono">{mlAvailable ? ml.data?.stage ?? "unavailable" : "unavailable"}</div></div>
            <div><div className="text-[10px] font-mono text-faint">LABELS</div><div className="mt-1 text-[12px] font-mono">{dataset ? `${dataset.samples}/${dataset.min_to_train}` : "—"}</div></div>
            <div><div className="text-[10px] font-mono text-faint">WIN RATE</div><div className="mt-1 text-[12px] font-mono">{dataset?.win_rate_pct == null ? "—" : `${dataset.win_rate_pct.toFixed(1)}%`}</div></div>
            <div><div className="text-[10px] font-mono text-faint">FEATURES</div><div className="mt-1 text-[12px] font-mono">{mlAvailable ? ml.data?.foundation.feature_count ?? "—" : "—"}</div></div>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            {(mlAvailable ? ml.data?.stages ?? [] : []).map((stage) => (
              <TerminalBadge key={stage.key} tone={stage.done ? "neutral" : stage.active ? "info" : "warn"}>
                {stage.label} · {stage.done ? "done" : stage.active ? "active" : "locked"}
              </TerminalBadge>
            ))}
          </div>
          <div className="mt-4 rounded-md border border-line px-3 py-3 text-[11px] text-dim">
            <div className="flex items-center justify-between gap-3">
              <span><strong className="text-txt">River shadow</strong> · delayed after-cost labels · drift alerts only</span>
              <TerminalBadge tone={mlAvailable && ml.data?.online_shadow?.active ? "info" : "neutral"}>{!mlAvailable ? "unavailable" : ml.data?.online_shadow?.active ? "shadow active" : "not configured"}</TerminalBadge>
            </div>
            <div className="mt-1 font-mono text-[10px] text-faint">{!mlAvailable ? "status artifact unavailable" : `${ml.data?.online_shadow?.installed ? "optional library installed" : "optional library not installed"} · ${ml.data?.online_shadow?.drift_supervisor?.configured_streams ?? 0} drift streams`} · non-binding · cannot trade</div>
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
            <TerminalBadge tone={agentFreshness.state === "OK" ? "neutral" : "warn"}>{agentFreshness.state === "OK" ? "research only" : `${agentFreshness.state.toLowerCase()} · ${agentFreshness.age}`}</TerminalBadge>
          </div>
          {!agentAvailable && <div className="mt-4 rounded-md border border-short/40 bg-short/5 px-3 py-2 text-[11px] text-short" role="alert">Agent governor artifact unavailable. Zero actions or tasks is not being asserted.</div>}
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div><div className="text-[10px] font-mono text-faint">ACTIONS</div><div className="mt-1 text-[12px] font-mono">{summary?.operator_actions ?? "—"}</div></div>
            <div><div className="text-[10px] font-mono text-faint">CRITICAL</div><div className="mt-1 text-[12px] font-mono text-short">{summary?.critical_actions ?? "—"}</div></div>
            <div><div className="text-[10px] font-mono text-faint">ACTIVE TASKS</div><div className="mt-1 text-[12px] font-mono">{summary?.gateway_active_tasks ?? "—"}</div></div>
            <div><div className="text-[10px] font-mono text-faint">MIN HEALTH</div><div className="mt-1 text-[12px] font-mono">{summary?.agent_health_min == null ? "—" : `${summary.agent_health_min.toFixed(0)}/100`}</div></div>
          </div>
          <div className="mt-4 rounded-md border border-line px-3 py-3 text-[11px] text-dim">
            {agents.data?.operator_answer ?? (agents.isError ? "Agent status endpoint unavailable." : "Agent artifact not populated.")}
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {(agentAvailable ? agents.data?.source_status ?? [] : []).map((source) => (
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

type FreshnessRow = { name: string; age: string; state: "OK" | "STALE" | "MISSING" | "UNKNOWN" | "HISTORICAL"; sla: string; required?: boolean };

function artifactFreshness(name: string, artifact?: ArtifactMetadata): FreshnessRow {
  if (!artifact) return { name, age: "not reported", state: "MISSING", sla: "declared by producer", required: false };
  const state = artifact.state === "CURRENT" ? "OK" : artifact.state;
  return {
    name,
    age: artifact.age_seconds == null ? "not reported" : ageSec(artifact.age_seconds),
    state,
    sla: artifact.expected_interval_seconds == null ? "historical" : ageSec(artifact.expected_interval_seconds),
    required: false,
  };
}

function generatedFreshness(name: string, generatedAt: string | null | undefined, slaSeconds: number): FreshnessRow {
  if (!generatedAt) return { name, age: "not configured", state: "MISSING", sla: ageSec(slaSeconds), required: false };
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
  const readiness = useReadiness();
  const products = useDataProducts();
  const snapshotAge = typeof snapshot.data?.snapshot_age_ms === "number" ? snapshot.data.snapshot_age_ms / 1000 : null;
  const fallbackFreshness: FreshnessRow[] = [
    readiness.data?.status === "ready"
      ? { name: "workflow readiness", age: "current", state: "OK", sla: "10s", required: true }
      : { name: "workflow readiness", age: readiness.data?.reasons.join(", ") || "not reported", state: "MISSING", sla: "10s", required: true },
    snapshotAge == null
      ? { name: "runtime snapshot", age: "not reported", state: "MISSING", sla: "15s", required: true }
      : { name: "runtime snapshot", age: ageSec(snapshotAge), state: snapshotAge <= 15 ? "OK" : "STALE", sla: "15s", required: true },
    generatedFreshness("research scorecard", scorecard.data?.generated_at, 2 * 60 * 60),
    generatedFreshness("ML pipeline", ml.data?.generated_at, 2 * 60 * 60),
    generatedFreshness("agent governor", agents.data?.generated_at, 2 * 60 * 60),
  ];
  const freshness: FreshnessRow[] = products.data?.rows.map((row) => ({
    name: row.product.replace(/_/g, " "),
    age: row.age_seconds == null ? "not reported" : ageSec(row.age_seconds),
    state: row.state === "CURRENT" ? "OK" : row.state,
    sla: row.expected_interval_seconds == null ? "historical" : ageSec(row.expected_interval_seconds),
    required: row.required,
  })) ?? fallbackFreshness;
  const runtimeNonOk = freshness.filter((row) => row.required && row.state !== "OK").length;
  const optionalUnavailable = freshness.filter((row) => !row.required && row.state !== "OK").length;
  const cols: Column<FreshnessRow>[] = [
    { key: "artifact", header: "Artifact", render: (row) => <span className="font-mono">{row.name}</span> },
    { key: "age", header: "Age", align: "right", render: (row) => row.age },
    { key: "sla", header: "SLA", align: "right", render: (row) => row.sla },
    { key: "state", header: "State", render: (row) => <TerminalBadge tone={row.state === "OK" ? "good" : row.state === "HISTORICAL" ? "info" : row.required ? (row.state === "STALE" || row.state === "UNKNOWN" ? "warn" : "bad") : "neutral"}>{row.state === "MISSING" && !row.required ? "NOT CONFIGURED" : row.state}</TerminalBadge> },
  ];
  return (
    <div className="space-y-4">
      <TerminalPanel title="System" meta="freshness · feed · build · bad list">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Build" value={meta.data?.build_sha?.slice(0, 8) ?? "—"} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Host" value={meta.data?.host ?? "—"} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Uptime" value={ageSec(meta.data?.uptime_seconds)} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Runtime non-OK" value={String(runtimeNonOk)} tone={runtimeNonOk ? "text-short" : "text-long"} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Disk used" value={meta.data?.disk ? `${meta.data.disk.used_pct.toFixed(1)}%` : "—"} tone={(meta.data?.disk?.used_pct ?? 0) > 85 ? "text-short" : ""} /></div>
          <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Load 1m / CPU" value={meta.data?.load_average?.["1m"] == null ? "—" : `${meta.data.load_average["1m"].toFixed(2)} / ${meta.data.cpu_count ?? "—"}`} /></div>
        </div>
        <div className="mt-3 flex items-center gap-2 text-[10px] text-faint"><TerminalBadge tone={optionalUnavailable ? "neutral" : "good"}>{optionalUnavailable} optional unavailable</TerminalBadge><span>Research, ML, and agent artifacts never determine runtime health.</span></div>
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
              <div className="flex justify-between gap-3"><span className="text-dim">browser transport</span><TerminalBadge tone={meta.data?.transport?.secure ? "good" : "bad"}>{meta.data?.transport?.secure ? "HTTPS" : "NOT SECURE"}</TerminalBadge></div>
              <div className="flex justify-between gap-3"><span className="text-dim">runtime</span><span className="font-mono text-dim">pid {meta.data?.process_id ?? "—"} · py {meta.data?.python ?? "—"}</span></div>
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
  const { data, isLoading, isError } = useSnapshot();
  const rows = (data?.positions as Position[] | undefined) ?? [];
  const runtimeMode = String(data?.mode ?? "").toLowerCase();
  const cols: Column<Position>[] = [
    { key: "sym", header: "Symbol", render: (r) => <span className="font-mono">{r.symbol ?? "—"}</span> },
    { key: "side", header: "Side", render: (r) => r.side ?? "—" },
    { key: "qty", header: "Qty", align: "right", render: (r) => (typeof r.quantity === "number" ? r.quantity : "—") },
    { key: "entry", header: "Entry", align: "right", render: (r) => priceText(r.entry_price) },
    { key: "mark", header: "Mark", align: "right", render: (r) => priceText(r.mark_price) },
    { key: "notional", header: "Notional", align: "right", render: (r) => usd(r.notional_usd) },
    { key: "margin", header: "Margin / Lev", align: "right", render: (r) => <span>{usd(r.margin_usd)} · {typeof r.effective_leverage === "number" ? `${r.effective_leverage.toFixed(1)}x` : "—"}</span> },
    { key: "risk", header: "SL / TP / Liq", render: (r) => <span className="whitespace-nowrap font-mono text-[10px]">{priceText(r.stop_price)} / {priceText(r.take_profit_price)} / {priceText(r.liquidation_price)}</span> },
    {
      key: "upnl",
      header: "uPnL",
      align: "right",
      render: (r) => <span className={signed(r.unrealized_pnl_usd)}>{usd(r.unrealized_pnl_usd)}</span>,
    },
    { key: "excursion", header: "MFE / MAE", align: "right", render: (r) => `${usd(r.mfe_usd)} / ${usd(r.mae_usd)}` },
    { key: "age", header: "Age", align: "right", render: (r) => ageSec(r.age_seconds) },
  ];
  if (!isLoading && !isError && rows.length === 0 && !["paper", "live", "live_small", "live_full"].includes(runtimeMode)) return null;
  return (
    <TerminalPanel title="Positions" meta={isLoading ? "loading…" : isError ? "unknown" : `${rows.length} open`}>
      {isError && <div className="mb-3 rounded-md border border-short/40 bg-short/5 px-3 py-2 text-[11px] text-short" role="alert">Position snapshot unavailable. Flat state is not being asserted.</div>}
      <DenseTable columns={cols} rows={rows} rowKey={(row, index) => `${row.symbol ?? "position"}-${index}`} empty={isLoading ? "loading positions…" : isError ? "positions unknown" : "flat — no open positions"} />
      {!rows.length && !isLoading && !isError && <div className="mt-3 text-[10px] text-faint">Shadow intents and virtual outcomes are shown in Desk; this table is reserved for venue/paper positions.</div>}
    </TerminalPanel>
  );
}

const num = (n: unknown, d = 2) => (typeof n === "number" ? n.toFixed(d) : "—");

export function MarketPanel() {
  const { data, isLoading, isError } = useSnapshot();
  const p = data?.price ?? null;
  const fr = data?.funding_rate;
  return (
    <TerminalPanel title="Market" meta={isLoading ? "loading…" : isError ? "unknown" : (data?.symbol as string) ?? "—"}>
      {p ? (
        <div className="flex items-end gap-10 flex-wrap">
          <Kpi label="Mid" value={priceText(p.mid)} />
          <Kpi label="Bid" value={priceText(p.bid)} />
          <Kpi label="Ask" value={priceText(p.ask)} />
          <Kpi label="Spread" value={`${num(p.spread_bps, 1)} bps`} />
          <Kpi
            label="Funding"
            value={typeof fr === "number" ? `${(fr * 100).toFixed(4)}%` : "—"}
            tone={typeof fr === "number" ? signed(fr) : ""}
          />
        </div>
      ) : (
        <div className={`text-[12px] font-mono ${isError ? "text-short" : "text-dim"}`} role={isError ? "alert" : undefined}>{isError ? "market snapshot unavailable — quote unknown" : isLoading ? "loading live quote…" : "no live quote (warming / no book)"}</div>
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
  const pageSize = 100;
  const [offset, setOffset] = useState(0);
  const { data, isLoading } = useJournal(pageSize, offset);
  const [view, setView] = useState<"all" | "scanner" | "decisions" | "trades">("all");
  const [search, setSearch] = useState("");
  const [laneFilter, setLaneFilter] = useState("all");
  const rows = data?.closed_trades ?? [];
  const summary = data?.summary;
  const actualRows = rows.filter((row) => row.kind === "actual_closing_fill" && row.performance_eligible === true);
  const rowsNet = actualRows.reduce((total, row) => {
    const value = row.net_after_this_fill_fee_usd ?? row.net_pnl_usd;
    return total + (typeof value === "number" && Number.isFinite(value) ? value : 0);
  }, 0);
  const summaryNet = summary?.actual_closed_net_usd;
  const mixedEntryClocks = summary?.mixed_entry_clock_headline === true;
  const executionContracts = Object.entries(summary?.execution_contract_pnl ?? {});
  const shadowHistoryComplete = summary?.shadow_history_complete === true;
  const reconciliationDelta = typeof summaryNet === "number" ? rowsNet - summaryNet : null;
  const completeTradePopulation = (data?.page?.totals.closed_trades ?? rows.length) <= rows.length;
  const reconciliationMismatch = completeTradePopulation && reconciliationDelta != null && Math.abs(reconciliationDelta) > 0.01;
  const normalizedSearch = search.trim().toLowerCase();
  const laneNames = Array.from(new Set([
    ...rows.map((row) => String(row.lane ?? row.symbol ?? "")).filter(Boolean),
    ...(data?.events ?? []).map((row) => String(row.lane ?? "")).filter(Boolean),
    ...(data?.scanner_events ?? []).map((row) => row.lane).filter(Boolean),
  ])).sort();
  const matches = (value: unknown) => !normalizedSearch || String(JSON.stringify(value) ?? "").toLowerCase().includes(normalizedSearch);
  const filteredRows = rows.filter((row) => (laneFilter === "all" || String(row.lane ?? row.symbol ?? "") === laneFilter) && matches(row));
  const filteredEvents = (data?.events ?? []).filter((row) => (laneFilter === "all" || String(row.lane ?? "") === laneFilter) && matches(row));
  const filteredScannerEvents = (data?.scanner_events ?? []).filter((row) => (laneFilter === "all" || row.lane === laneFilter) && matches(row));
  const scannerEventRows = filteredScannerEvents.map((row) => ({
    lane: row.lane,
    ts: row.ts,
    event: `scanner_${row.kind}`,
    detail: `${row.strategy_id} · ${row.reason} · ${row.timeframe}${row.backfill ? " · backfill" : ""}`,
  }));
  const decisionEvents = filteredEvents.filter((row) => /reject|block|refus|risk|skip/i.test(`${row.event ?? ""} ${row.detail ?? ""}`));
  const visibleEvents = view === "scanner" ? scannerEventRows : view === "decisions" ? decisionEvents : filteredEvents;
  const decisionTimes = [...(data?.events ?? []).map((row) => row.ts), ...(data?.scanner_events ?? []).map((row) => row.ts)].filter((value): value is string => Boolean(value)).sort();
  const lastDecisionTs = decisionTimes[decisionTimes.length - 1];
  const cols: Column<JournalRow>[] = [
    { key: "lane", header: "Lane", render: (r) => <span className="font-mono">{r.lane ?? r.symbol ?? "—"}</span> },
    { key: "path", header: "Path / evidence", render: (r) => <span className="block font-mono">{r.execution_contract_id ?? r.path_id ?? "legacy"}<span className={`block text-[9px] ${r.performance_eligible ? "text-long" : "text-warn"}`}>{r.performance_eligible ? "performance eligible" : "evidence only"}{r.decision_id ? ` · ${r.decision_id}` : ""}{r.permission_snapshot_id ? ` · ${r.permission_snapshot_id}` : ""}</span></span> },
    { key: "side", header: "Side", render: (r) => r.side ?? "—" },
    {
      key: "pnl",
      header: "Net PnL",
      align: "right",
      render: (r) => {
        const net = r.net_pnl_usd ?? r.net_after_this_fill_fee_usd ?? r.virtual_net_usd;
        return <span className={r.performance_eligible ? signed(net) : "text-faint"}>{r.performance_eligible ? usd(net) : "evidence only"}</span>;
      },
    },
    { key: "exit", header: "Exit", render: (r) => <span className="text-dim">{r.exit_reason ?? r.resolution ?? "—"}</span> },
  ];
  const exportEvidence = () => {
    if (!data) return;
    const payload = JSON.stringify({ ...data, closed_trades: filteredRows, events: filteredEvents, scanner_events: filteredScannerEvents }, null, 2);
    const blob = new Blob([payload], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `vnedge-journal-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  };
  return (
    <TerminalPanel title="Journal · evidence blotter" meta={isLoading ? "loading…" : `${data?.page?.totals.closed_trades ?? rows.length} closed · page ${Math.floor(offset / pageSize) + 1} · 20s`}>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="rounded-lg border border-line bg-inset p-3">
          <Kpi
            label={mixedEntryClocks ? "Paper net · split" : "Paper net"}
            value={mixedEntryClocks ? "SPLIT BY CLOCK" : usd(summary?.headline_actual_closed_net_usd)}
          />
          {mixedEntryClocks ? <div className="mt-1 text-[9px] font-mono text-warn">Use execution-contract cohorts; aggregate is audit-only.</div> : null}
        </div>
        <div className="rounded-lg border border-line bg-inset p-3">
          <Kpi label="Paper fees" value={usd(summary?.fees_usd)} />
          <div className="mt-1 text-[9px] font-mono text-faint">shadow modeled {usd(summary?.shadow_execution_fees_usd)}</div>
        </div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Shadow net" value={usd(summary?.virtual_net_usd)} /></div>
        <div className="rounded-lg border border-line bg-inset p-3"><Kpi label="Open orders" value={String(summary?.open_orders ?? 0)} /></div>
      </div>
      <div className={`mt-3 rounded-lg border px-3 py-2 text-[11px] ${reconciliationMismatch || !shadowHistoryComplete ? "border-warn/50 bg-warn/5 text-warn" : "border-line bg-inset text-dim"}`}>
        <span className="font-mono">Evidence reconciliation:</span> paper {completeTradePopulation ? `visible ${usd(rowsNet)} · ledger ${usd(summaryNet)}` : `ledger ${usd(summaryNet)} · page ${usd(rowsNet)}`}
        {completeTradePopulation ? (reconciliationMismatch ? ` · delta ${usd(reconciliationDelta)}` : " · matched") : " · paged"}
        {` · shadow ${summary?.shadow_closed_trades ?? 0} closed / ${usd(summary?.virtual_net_usd)} · ${shadowHistoryComplete ? "full-stream matched" : `history ${summary?.shadow_history_state ?? "unavailable"}`}`}
      </div>
      {executionContracts.length > 0 ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {executionContracts.map(([contractId, contract]) => (
            <div key={contractId} className="rounded-lg border border-line bg-inset px-3 py-2">
              <div className="break-all font-mono text-[10px] text-brand">{contractId}</div>
              <div className={`mt-1 font-mono text-sm ${signed(contract.net_usd)}`}>{usd(contract.net_usd)}</div>
              <div className="text-[9px] font-mono text-faint">{contract.closed} closed · {contract.win_rate_pct}% win</div>
            </div>
          ))}
        </div>
      ) : null}
      <div className="mt-4 flex flex-wrap items-center gap-2" role="group" aria-label="Journal view">
        {(["all", "scanner", "decisions", "trades"] as const).map((item) => <button key={item} onClick={() => setView(item)} className={`rounded-md border px-3 py-1.5 text-[10px] font-mono uppercase ${view === item ? "border-brand/50 bg-brand/10 text-brand" : "border-line text-dim"}`}>{item}</button>)}
        <input aria-label="Search journal" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="search event, reason, lane…" className="min-w-[220px] border border-line bg-inset px-2.5 py-1.5 font-mono text-[10px] text-txt placeholder:text-faint focus:border-brand focus:outline-none" />
        <select aria-label="Filter journal lane" value={laneFilter} onChange={(event) => setLaneFilter(event.target.value)} className="border border-line bg-inset px-2.5 py-1.5 font-mono text-[10px] text-dim focus:border-brand focus:outline-none"><option value="all">all lanes</option>{laneNames.map((lane) => <option key={lane} value={lane}>{lane}</option>)}</select>
        <button onClick={exportEvidence} disabled={!data} className="border border-line px-2.5 py-1.5 font-mono text-[10px] uppercase text-dim hover:border-brand hover:text-brand disabled:opacity-40">export evidence</button>
        <button onClick={() => setOffset((value) => Math.max(0, value - pageSize))} disabled={!data?.page?.has_previous || isLoading} className="border border-line px-2.5 py-1.5 font-mono text-[10px] uppercase text-dim hover:border-brand hover:text-brand disabled:opacity-30">newer</button>
        <button onClick={() => setOffset((value) => value + pageSize)} disabled={!data?.page?.has_more || isLoading} className="border border-line px-2.5 py-1.5 font-mono text-[10px] uppercase text-dim hover:border-brand hover:text-brand disabled:opacity-30">older</button>
        <span className="ml-auto text-[10px] font-mono text-faint">append-only · generated {data?.generated_at ? ageSec((Date.now() - Date.parse(data.generated_at)) / 1000) : "—"} ago</span>
      </div>
      <div className="mt-4 grid gap-3 xl:grid-cols-2">
        <div className="rounded-lg border border-line bg-inset p-3">
          <div className="mb-2 font-mono text-[10px] uppercase text-faint">{view === "scanner" ? `Scanner audit · ${filteredScannerEvents.length} records` : view === "decisions" ? "Decision rejects / arm blocks" : "Recent event stream"}</div>
          {visibleEvents.slice(0, 12).map((event, index) => <div key={`${event.ts}-${index}`} className="grid grid-cols-[72px_minmax(0,1fr)] gap-2 border-t border-line/60 py-2 first:border-0 text-[11px]"><span className="font-mono text-faint">{event.ts ? new Date(event.ts).toLocaleTimeString("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"}</span><span><span className="font-mono text-txt">{event.event ?? "decision"}</span><span className="ml-2 text-dim break-words">{event.detail ?? "no detail"}</span></span></div>)}
          {!visibleEvents.length && <div className="text-[11px] text-dim">No matching event records in the current journal window.</div>}
        </div>
        <div className="rounded-lg border border-line bg-inset p-3">
          <div className="font-mono text-[10px] uppercase text-faint">Decision log freshness</div>
          <div className="mt-2 text-[12px] text-dim">Last decision record</div>
          <div className="mt-1 font-mono text-lg">{lastDecisionTs ? ageSec((Date.now() - Date.parse(lastDecisionTs)) / 1000) : "not observed"}</div>
          <div className="mt-2 text-[10px] text-faint">An empty journal is explicit; it is not evidence of a healthy decision path.</div>
        </div>
      </div>
      {(view === "all" || view === "trades") && <div className="mt-4"><DenseTable columns={cols} rows={filteredRows} empty="no closed trades match the current evidence filter" /></div>}
    </TerminalPanel>
  );
}
