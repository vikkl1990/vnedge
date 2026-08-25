import { useEffect, useMemo, useState } from "react";
import type { CorrectionLane, RiskSnapshot } from "../api";
import { TerminalBadge } from "./Terminal";

const age = (seconds: number | null | undefined) => {
  if (seconds == null || !Number.isFinite(seconds)) return "not observed";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5_400) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3_600).toFixed(1)}h`;
};

const usd = (value: number | null | undefined) =>
  value == null || !Number.isFinite(value)
    ? "—"
    : `${value < 0 ? "−" : value > 0 ? "+" : ""}$${Math.abs(value).toFixed(2)}`;

const numberFrom = (record: Record<string, unknown> | null, keys: string[]) => {
  for (const key of keys) {
    const value = record?.[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
};

const objectFrom = (value: unknown): Record<string, unknown> | null =>
  value != null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;

const listFrom = (value: unknown): string[] =>
  Array.isArray(value) ? value.map(String) : [];

type Tone = "neutral" | "good" | "warn" | "bad" | "info";

function healthTone(health: CorrectionLane["health"]): Tone {
  return health === "ok" ? "good" : health === "degraded" ? "warn" : health === "blocked" ? "bad" : "neutral";
}

function Criterion({ label, value, detail, tone }: { label: string; value: string; detail: string; tone: Tone }) {
  const light = tone === "good" ? "bg-long" : tone === "warn" ? "bg-warn" : tone === "bad" ? "bg-short" : tone === "info" ? "bg-info" : "bg-faint";
  return (
    <div className="grid grid-cols-[10px_118px_minmax(0,1fr)_auto] items-center gap-2 border-t border-line/60 py-2 first:border-0">
      <span className={`h-2 w-2 rounded-sm ${light}`} />
      <span className="font-mono text-[10px] uppercase tracking-wide text-faint">{label}</span>
      <span className="truncate text-[11px] text-dim" title={detail}>{detail}</span>
      <TerminalBadge tone={tone}>{value}</TerminalBadge>
    </div>
  );
}

function Funnel({ lane }: { lane: CorrectionLane }) {
  const stages = [
    ["closed bars", lane.funnel.bars ?? 0],
    ["evaluations", lane.funnel.evals ?? 0],
    ["signals", lane.funnel.signals ?? 0],
    ["cost/risk survived", lane.funnel.shadow_approved ?? 0],
    ["open / pending", lane.open_positions + (lane.shadow_perf?.pending_shadow_intents ?? 0)],
    ["resolved", (lane.shadow_perf?.wins ?? 0) + (lane.shadow_perf?.losses ?? 0)],
  ] as const;
  const ceiling = Math.max(1, ...stages.map(([, value]) => value));
  return (
    <div className="space-y-2">
      {stages.map(([label, value]) => (
        <div key={label}>
          <div className="mb-1 flex items-center justify-between font-mono text-[10px]"><span className="text-faint">{label}</span><span>{value.toLocaleString("en-US")}</span></div>
          <div className="h-1.5 overflow-hidden bg-line/60"><div className="h-full bg-brand/80" style={{ width: `${Math.max(value ? 3 : 0, value / ceiling * 100)}%` }} /></div>
        </div>
      ))}
    </div>
  );
}

export function ScannerWorkspace({
  lanes,
  selectedSymbol,
  shadowPurse,
  streamState,
  risk,
  conflicts,
}: {
  lanes: CorrectionLane[];
  selectedSymbol: string;
  shadowPurse: number | null;
  streamState: "connecting" | "live" | "retrying";
  risk?: RiskSnapshot;
  conflicts?: Record<string, { state: string; selected: string | null; side?: string }>;
}) {
  const scanners = useMemo(
    () => lanes.filter((lane) => lane.observation_class === "shadow_observe"),
    [lanes],
  );
  const preferred = scanners.find((lane) => lane.symbol.replace(/[^A-Z]/g, "").startsWith(selectedSymbol.replace("USDT", "")));
  const [selectedId, setSelectedId] = useState<string | null>(preferred?.lane_id ?? scanners[0]?.lane_id ?? null);
  useEffect(() => {
    if (preferred) setSelectedId(preferred.lane_id);
  }, [preferred?.lane_id]);
  const selected = scanners.find((lane) => lane.lane_id === selectedId) ?? preferred ?? scanners[0];
  const outcomes = selected?.shadow_perf?.shadow_outcomes_recent ?? [];
  const latestOutcome = outcomes[0] ?? null;
  const outcomeNet = numberFrom(latestOutcome, ["virtual_net_usd", "net_pnl_usd", "pnl_usd"]);
  const closeWarm = selected ? selected.latency_samples.bar_close >= selected.latency_samples.required : false;
  const decisionWarm = selected ? selected.latency_samples.decision >= selected.latency_samples.required : false;
  const dataTone: Tone = selected?.candle_status === "ok" ? "good" : selected?.candle_status?.includes("block") ? "bad" : "warn";
  const evaluation = selected?.last_eval ?? null;
  const lifecycle = String(evaluation?.setup_lifecycle ?? "watching").replace(/_/g, " ");
  const failedGates = listFrom(evaluation?.all_failed_gates);
  const nearMiss = objectFrom(evaluation?.near_miss);
  const symbolConflict = conflicts?.[selected?.symbol ?? ""];
  const quoteEngine = selected?.runtime_contract?.decision_engine?.startsWith("quote_acceptance") ?? false;
  const quoteStats = selected?.shadow_perf;
  const quoteDistinct = quoteStats?.quotes_distinct ?? 0;
  const quoteSeen = quoteStats?.quotes_seen ?? 0;
  const quoteRejects = quoteStats?.quote_contract_rejects ?? 0;
  const quoteDrops = quoteStats?.quote_overflow_drops ?? 0;

  return (
    <section className="border border-line bg-panel/80" aria-label="Scanner decision workspace">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-inset/70 px-3 py-2">
        <div className="flex items-center gap-3">
          <div><div className="font-mono text-[10px] uppercase tracking-[0.16em] text-faint">Decision workspace</div><div className="text-[11px] text-dim">coverage → criteria → funnel → outcome</div></div>
          <TerminalBadge tone="info">shadow only</TerminalBadge>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <TerminalBadge tone={streamState === "live" ? "good" : "warn"}>pulse ws {streamState}</TerminalBadge>
          <TerminalBadge tone={scanners.length ? "info" : "neutral"}>{scanners.length} lanes</TerminalBadge>
          <TerminalBadge tone="neutral">{scanners.reduce((sum, lane) => sum + (lane.shadow_perf?.pending_shadow_intents ?? 0), 0)} pending</TerminalBadge>
          <TerminalBadge tone="info">purse {shadowPurse == null ? "—" : `$${shadowPurse.toFixed(0)}`}</TerminalBadge>
          <TerminalBadge tone={symbolConflict?.state === "conflict" ? "bad" : symbolConflict ? "good" : "neutral"}>arbiter {symbolConflict?.state ?? "empty"}</TerminalBadge>
        </div>
      </header>

      {!selected ? <div className="p-4 text-[12px] text-dim">No shadow scanner lane is reported. Measurement remains available.</div> : (
        <div className="grid xl:grid-cols-[300px_minmax(420px,1fr)_290px]">
          <div className="border-b border-line p-3 xl:border-b-0 xl:border-r">
            <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Coverage queue</div>
            <div className="space-y-1">
              {scanners.map((lane) => {
                const active = lane.lane_id === selected.lane_id;
                return (
                  <button key={lane.lane_id} onClick={() => setSelectedId(lane.lane_id)} className={`w-full border px-2.5 py-2 text-left ${active ? "border-brand/60 bg-brand/10" : "border-line bg-inset/50 hover:border-line2"}`}>
                    <div className="flex items-center justify-between gap-2"><span className="font-mono text-[11px]">{lane.symbol} · {lane.timeframe}</span><TerminalBadge tone={healthTone(lane.health)}>{lane.health}</TerminalBadge></div>
                    <div className="mt-1 truncate text-[10px] text-dim">{lane.strategy_id}</div>
                    <div className="mt-1 flex justify-between font-mono text-[10px] text-faint"><span>signal {age(lane.last_signal_age_seconds)}</span><span>{usd(lane.shadow_perf?.virtual_net_usd)}</span></div>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="border-b border-line p-3 xl:border-b-0 xl:border-r">
            <div className="mb-2 flex items-center justify-between gap-3"><div className="font-mono text-[10px] uppercase tracking-wide text-faint">Arm criteria · current evidence</div><span className="font-mono text-[10px] text-dim">{selected.symbol} / {selected.timeframe}</span></div>
            <Criterion label="data" value={selected.candle_status} detail={selected.candle_age_ms == null ? "age not reported" : `${Math.round(selected.candle_age_ms / 1000)}s since candle evidence`} tone={dataTone} />
            <Criterion label="close path" value={closeWarm ? "measured" : "collecting p95"} detail={selected.bar_close_processing_ms == null ? `${selected.latency_samples.bar_close}/${selected.latency_samples.required} persisted samples` : `p95 ${selected.bar_close_processing_ms.toFixed(1)} ms · ${selected.latency_samples.bar_close}/${selected.latency_samples.required}`} tone={closeWarm ? "good" : "warn"} />
            <Criterion label="decision" value={decisionWarm ? "measured" : "collecting p95"} detail={selected.decision_lag_ms == null ? `${selected.latency_samples.decision}/${selected.latency_samples.required} persisted samples` : `p95 ${selected.decision_lag_ms.toFixed(1)} ms · ${selected.latency_samples.decision}/${selected.latency_samples.required}`} tone={decisionWarm ? "good" : "warn"} />
            <Criterion label="cost wall" value={selected.round_trip_bps == null ? "unknown" : `${selected.round_trip_bps.toFixed(1)} bps`} detail={`${selected.cost_profile} · never bypassed by scanner state`} tone={selected.round_trip_bps == null ? "warn" : "info"} />
            <Criterion label="engine path" value={selected.runtime_contract?.decision_engine ?? "unreported"} detail={`exit ${selected.runtime_contract?.exit_engine ?? "unreported"} · ${selected.runtime_contract?.max_holding_bars ?? "—"} bars`} tone={selected.runtime_contract?.decision_engine && selected.runtime_contract?.exit_engine ? "info" : "warn"} />
            {quoteEngine && <Criterion
              label="quote tape"
              value={quoteDistinct ? `${quoteDistinct}/${quoteSeen}` : "no samples"}
              detail={`${quoteRejects} rejected · ${quoteDrops} overflow drops · ${quoteStats?.quote_rearms ?? 0} re-arms · ${quoteStats?.quote_source ?? "unknown source"}`}
              tone={!quoteDistinct ? "warn" : quoteDrops ? "warn" : "good"}
            />}
            <Criterion label="risk" value={risk?.kill.active || risk?.daily_halt.active ? "blocked" : "clear"} detail={risk?.kill.active ? "kill is latched" : risk?.daily_halt.active ? "daily halt active" : "kill and daily halt clear; capital still disabled"} tone={risk?.kill.active || risk?.daily_halt.active ? "bad" : "good"} />
            <Criterion label="last result" value={outcomeNet == null ? "none" : usd(outcomeNet)} detail={latestOutcome ? String(latestOutcome.reason ?? latestOutcome.resolution ?? "resolved shadow observation") : "no recent resolved outcome"} tone={outcomeNet == null ? "neutral" : outcomeNet >= 0 ? "good" : "bad"} />
            <div className="mt-3 grid gap-2 md:grid-cols-2">
              <div className="border-l-2 border-brand bg-brand/5 px-3 py-2"><div className="font-mono text-[10px] uppercase text-brand">Setup lifecycle</div><div className="mt-1 text-[12px] uppercase text-main">{lifecycle}</div><div className="mt-1 text-[10px] text-faint">observation only · cannot grant permission</div></div>
              <div className="border-l-2 border-warn bg-warn/5 px-3 py-2"><div className="font-mono text-[10px] uppercase text-warn">Why not armed</div><div className="mt-1 text-[11px] text-dim">{failedGates[0] || selected.current_waiting_reason || selected.why_no_fire}</div>{failedGates.length > 1 && <div className="mt-1 font-mono text-[9px] text-faint">+{failedGates.length - 1} additional gates</div>}</div>
            </div>
            <div className="mt-2 border border-line bg-inset/50 px-3 py-2">
              <div className="flex items-center justify-between gap-3"><span className="font-mono text-[10px] uppercase text-faint">Nearest counterfactual</span><TerminalBadge tone="neutral">research only</TerminalBadge></div>
              <div className="mt-1 text-[11px] text-dim">{nearMiss ? String(nearMiss.would_pass_if ?? "distance unavailable") : "No threshold-distance evidence yet."}</div>
              {failedGates.length > 0 && <div className="mt-2 flex flex-wrap gap-1">{failedGates.slice(0, 6).map((gate) => <TerminalBadge key={gate} tone="warn">{gate}</TerminalBadge>)}</div>}
            </div>
          </div>

          <div className="p-3">
            <div className="mb-3 flex items-center justify-between"><div className="font-mono text-[10px] uppercase tracking-wide text-faint">Conversion funnel</div><TerminalBadge tone={healthTone(selected.health)}>{selected.health}</TerminalBadge></div>
            <Funnel lane={selected} />
            <div className="mt-4 border-t border-line pt-3">
              <div className="font-mono text-[10px] uppercase text-faint">Virtual performance</div>
              <div className="mt-2 grid grid-cols-3 gap-2 text-center font-mono"><div><div className="text-lg">{usd(selected.shadow_perf?.virtual_net_usd)}</div><div className="text-[9px] text-faint">NET</div></div><div><div className="text-lg text-long">{selected.shadow_perf?.wins ?? 0}</div><div className="text-[9px] text-faint">WINS</div></div><div><div className="text-lg text-short">{selected.shadow_perf?.losses ?? 0}</div><div className="text-[9px] text-faint">LOSSES</div></div></div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
