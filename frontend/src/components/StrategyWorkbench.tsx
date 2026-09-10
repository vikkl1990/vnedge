import { Suspense, lazy, useEffect, useMemo } from "react";
import type { CorrectionLane, ScannerAuditEvent } from "../api";
import { useJournal, useLanes } from "../queries";
import { useUi } from "../store";
import { TerminalBadge } from "./Terminal";

const ScannerChart = lazy(() => import("./ScannerChart").then((module) => ({ default: module.ScannerChart })));

const age = (seconds: number | null | undefined) => {
  if (seconds == null || !Number.isFinite(seconds)) return "never";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5_400) return `${(seconds / 60).toFixed(1)}m`;
  if (seconds < 172_800) return `${(seconds / 3_600).toFixed(1)}h`;
  return `${(seconds / 86_400).toFixed(1)}d`;
};

const text = (value: unknown, fallback = "not reported") =>
  value == null || value === "" ? fallback : String(value).replace(/_/g, " ");

const booleanTone = (value: unknown): "info" | "bad" | "neutral" =>
  value === true || value === 1 ? "info" : value === false || value === 0 ? "bad" : "neutral";

const objectRecord = (value: unknown): Record<string, unknown> =>
  value != null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};

const firstReported = (...values: unknown[]) =>
  values.find((value) => value != null && value !== "");

export function regimeView(evaluation: Record<string, unknown>, lake: CorrectionLane["lake_contract"] = {}) {
  const features = objectRecord(evaluation.features);
  return {
    ready: firstReported(evaluation.mreg_ready, features.mreg_ready),
    state: firstReported(evaluation.mreg_state, features.regime_state, features.mreg_state),
    ema200Ready: firstReported(
      evaluation.mreg_ema200_ready,
      features.ema200_ready,
      features.mreg_ema200_ready,
      lake?.ema200_ready,
    ),
    dailyObservations: firstReported(
      evaluation.mreg_daily_observations,
      features.daily_observations,
      features.mreg_daily_observations,
      lake?.daily_bars,
    ),
    emaState: firstReported(evaluation.mreg_ema_state, features.regime_ema_state),
    macdImpulse: firstReported(
      evaluation.mreg_macd_impulse,
      features.regime_macd_impulse,
    ),
    rsiZone: firstReported(evaluation.mreg_rsi_zone, features.regime_rsi_zone),
  };
}

export function structureView(evaluation: Record<string, unknown>) {
  const features = objectRecord(evaluation.features);
  return {
    ready: firstReported(evaluation.bos15_structure_ready, features.bos15_structure_ready),
    oneHourTrend: firstReported(
      evaluation.bos15_structure_trend,
      features.bos15_structure_trend,
      features.structure_1h,
    ),
    fourHourTrend: firstReported(
      evaluation.bos15_htf_structure_trend,
      features.bos15_htf_structure_trend,
      features.htf_4h,
    ),
    parentIdentity: firstReported(
      evaluation.bos15_parent_identity_ok,
      features.bos15_parent_identity_ok,
    ),
    reason: firstReported(features.bos15_structure_health_reason),
    highCount: firstReported(features.bos15_confirmed_high_count),
    lowCount: firstReported(features.bos15_confirmed_low_count),
    lastReset: firstReported(features.bos15_last_quality_reset_at),
    barsSinceReset: firstReported(features.bos15_eligible_bars_since_reset),
    lastHighConfirmed: firstReported(features.bos15_last_high_confirmed_at),
    lastLowConfirmed: firstReported(features.bos15_last_low_confirmed_at),
    avwap: firstReported(
      evaluation.mreg_avwap_source,
      features.mreg_avwap_source,
      evaluation.bos15_dual_avwap_bias,
      features.bos15_dual_avwap_bias,
    ),
  };
}

function InspectorSection({ title, kicker, children }: { title: string; kicker: string; children: React.ReactNode }) {
  return (
    <section className="inspector-section">
      <div className="inspector-section__head"><span>{title}</span><span>{kicker}</span></div>
      {children}
    </section>
  );
}

function Fact({ label, value, tone = "neutral" }: { label: string; value: string; tone?: "neutral" | "good" | "warn" | "bad" | "info" }) {
  return (
    <div className="inspector-fact">
      <span>{label}</span>
      <strong title={value} className={`fact-tone fact-tone--${tone}`}>{value}</strong>
    </div>
  );
}

function Funnel({ lane }: { lane: CorrectionLane }) {
  const items = [
    ["Eval", lane.funnel.evals ?? 0],
    ["Setup", lane.lifecycle.armed_entries],
    ["Evidence", lane.lifecycle.candidates],
    ["Accept", lane.lifecycle.accepted],
    ["Submit", lane.funnel.submitted ?? 0],
  ] as const;
  return (
    <div className="funnel-strip">
      {items.map(([label, value], index) => (
        <div key={label} className="funnel-stage">
          <strong>{value}</strong><span>{label}</span>
          {index < items.length - 1 && <i aria-hidden="true" />}
        </div>
      ))}
    </div>
  );
}

function EvidenceTape({ lane, events }: { lane: CorrectionLane; events: ScannerAuditEvent[] }) {
  const visible = events.filter((event) => event.lane === lane.lane_id).slice(0, 8);
  return (
    <section className="elite-card evidence-tape">
      <div className="elite-card__header">
        <div><span className="eyebrow">Decision stream</span><h3>Immutable evidence</h3></div>
        <TerminalBadge tone={visible.some((row) => row.decision_id) ? "info" : "neutral"}>{visible.length} events</TerminalBadge>
      </div>
      <div className="evidence-tape__rows">
        {visible.map((event) => (
          <div key={`${event.ts}:${event.kind}:${event.decision_id ?? event.intent_key ?? "legacy"}`} className="evidence-tape__row">
            <span className={`stage-mark stage-mark--${event.kind}`} />
            <time>{new Date(event.ts).toLocaleTimeString("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
            <strong>{event.kind.toUpperCase()}</strong>
            <span className="evidence-tape__reason">{text(event.reason, "no reason")}</span>
            <code>{event.decision_id?.slice(0, 12) ?? "no decision id"}</code>
          </div>
        ))}
        {!visible.length && (
          <div className="empty-authority"><span className="empty-authority__glyph">∅</span><strong>System on, no envelope</strong><p>The lane is evaluating, but no decision identity exists in the current journal window.</p></div>
        )}
      </div>
    </section>
  );
}

export function StrategyWorkbench() {
  const lanesQuery = useLanes();
  const journal = useJournal(100, 0);
  const lanes = useMemo(() => (lanesQuery.data?.lanes ?? []).filter((lane) => lane.observation_class === "shadow_observe"), [lanesQuery.data]);
  const selectedId = useUi((state) => state.selectedLaneId);
  const setSelectedId = useUi((state) => state.setSelectedLane);
  const lane = lanes.find((item) => item.lane_id === selectedId) ?? lanes[0] ?? null;
  useEffect(() => {
    if (!selectedId && lanes[0]) setSelectedId(lanes[0].lane_id);
  }, [lanes, selectedId]);
  const evaluation = lane?.last_eval ?? {};
  const regime = regimeView(evaluation, lane?.lake_contract);
  const structure = structureView(evaluation);
  const contextAges = lane?.runtime_contract?.context_age_seconds ?? {};
  const gateCounts = lane?.drought?.primary_gate_counts_24h ?? {};
  const gateTotal = Object.values(gateCounts).reduce((sum, value) => sum + value, 0);
  const topGates = Object.entries(gateCounts).sort((a, b) => b[1] - a[1]).slice(0, 5);
  const lifecycleEvents = journal.data?.scanner_events ?? [];

  if (!lane) {
    return <div className="elite-card empty-authority"><span className="empty-authority__glyph">—</span><strong>No strategy lane is published</strong><p>The workstation will remain empty instead of inventing a primary system.</p></div>;
  }

  return (
    <div className="strategy-workbench">
      <section className="strategy-hero elite-card">
        <div className="strategy-hero__title">
          <span className="eyebrow">Active system</span>
          <h1>{lane.strategy_id}</h1>
          <p>One closed decision bar, one frozen permission, one evidence envelope.</p>
        </div>
        <div className="strategy-toolbar" role="group" aria-label="Strategy selection">
          <label><span>System</span><select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{lanes.map((item) => <option key={item.lane_id} value={item.lane_id}>{item.strategy_id} · {item.symbol}</option>)}</select></label>
          <div><span>Market</span><strong>{lane.symbol}</strong></div>
          <div><span>Scale</span><strong>{lane.timeframe}</strong></div>
          <div><span>Clock</span><strong>{text(lane.runtime_contract?.entry_clock)}</strong></div>
          <div><span>Mode</span><strong>{lane.mode}</strong></div>
          <div><span>Booked / wall</span><strong>{lane.execution_cost_bps?.toFixed(1) ?? "—"} / {lane.gate_cost_bps?.toFixed(1) ?? "—"} bps</strong></div>
        </div>
      </section>

      <Funnel lane={lane} />

      <div className="strategy-workbench__grid">
        <aside className="strategy-inspector elite-card">
          <div className="strategy-inspector__header">
            <div><span className="eyebrow">Decision anatomy</span><h2>{lane.symbol} · {lane.timeframe}</h2></div>
            <TerminalBadge tone={lane.health === "ok" ? "neutral" : lane.health === "degraded" ? "warn" : "bad"}>{lane.health}</TerminalBadge>
          </div>

          <InspectorSection title="Regime" kicker="permission">
            {lane.lake_contract?.evaluation_status === "awaiting_first_evaluation" &&
              <Fact label="Evaluation" value="Awaiting first closed-bar evaluation" tone="warn" />}
            <Fact label="Ready" value={text(regime.ready, text(lane.drought?.mreg_ready))} tone={booleanTone(regime.ready ?? lane.drought?.mreg_ready)} />
            <Fact label="State" value={text(regime.state, "flat / unknown")} tone={regime.state === "continuation" ? "info" : "neutral"} />
            <Fact label="EMA 200" value={text(regime.ema200Ready, lane.lake_contract?.evaluation_status === "awaiting_first_evaluation" ? "awaiting evaluation" : "not reported")} tone={booleanTone(regime.ema200Ready)} />
            <Fact label="Daily bars" value={text(regime.dailyObservations, "—")} />
            <Fact label="EMA · MACD · RSI" value={`${text(regime.emaState, "—")} · ${text(regime.macdImpulse, "—")} · ${text(regime.rsiZone, "—")}`} />
            <div className="context-age-grid">{(lane.runtime_contract?.context_tfs ?? []).map((tf) => <div key={tf}><span>{tf}</span><strong>{age(contextAges[tf])}</strong></div>)}</div>
          </InspectorSection>

          <InspectorSection title="Structure" kicker="geometry">
            <Fact label="Ready" value={text(structure.ready, text(lane.drought?.structure_ready))} tone={booleanTone(structure.ready ?? lane.drought?.structure_ready)} />
            <Fact label="1h trend" value={text(structure.oneHourTrend)} />
            <Fact label="4h trend" value={text(structure.fourHourTrend)} />
            <Fact label="Parent identity" value={text(structure.parentIdentity)} tone={booleanTone(structure.parentIdentity)} />
            <Fact label="Structure reason" value={text(structure.reason)} />
            <Fact label="Confirmed highs / lows" value={`${text(structure.highCount, "—")} / 2 · ${text(structure.lowCount, "—")} / 2`} />
            <Fact label="Last quality reset (UTC)" value={text(structure.lastReset, structure.barsSinceReset == null ? "not reported" : "none in frame")} />
            <Fact label="Eligible bars since reset" value={text(structure.barsSinceReset, "—")} />
            <Fact label="Last high confirmed (UTC)" value={text(structure.lastHighConfirmed, "none reported")} />
            <Fact label="Last low confirmed (UTC)" value={text(structure.lastLowConfirmed, "none reported")} />
            <Fact label="AVWAP" value={text(structure.avwap, "unused")} />
          </InspectorSection>

          <InspectorSection title="Drought" kicker={text(lane.drought?.drought_class, "unknown")}>
            <div className="age-quartet">
              <div><span>Eval</span><strong>{age(lane.drought?.eval_age_s)}</strong></div>
              <div><span>Setup</span><strong>{age(lane.drought?.setup_age_s)}</strong></div>
              <div><span>Evidence</span><strong>{age(lane.drought?.evidence_age_s)}</strong></div>
              <div><span>Accept</span><strong>{age(lane.drought?.accept_age_s)}</strong></div>
            </div>
            <div className="dominant-gate"><span>Why waiting</span><strong>{text(lane.drought?.last_primary_failed_gate, lane.current_waiting_reason)}</strong></div>
            <div className="gate-histogram">
              {topGates.map(([gate, count]) => <div key={gate}><span title={gate}>{gate.replace(/_/g, " ")}</span><i><b style={{ width: `${gateTotal ? Math.max(4, count / gateTotal * 100) : 0}%` }} /></i><strong>{count}</strong></div>)}
              {!topGates.length && <p>No 24h gate histogram reported.</p>}
            </div>
          </InspectorSection>

          <InspectorSection title="Contract" kicker="read only">
            <Fact label="Decision" value={`${lane.runtime_contract?.decision_tf ?? lane.timeframe} closed`} />
            <Fact label="Context" value={lane.runtime_contract?.context_tfs?.join(" · ") || "none"} />
            <Fact label="Transport" value={lane.decision_transport} tone={lane.decision_transport === "router" ? "good" : "warn"} />
            <Fact label="Source" value={lane.candle_source} />
            <Fact label="Path" value={lane.path_id} tone={lane.path_id === "kernel_v1" ? "info" : "warn"} />
            <Fact label="Snapshot" value={lane.permission_snapshot_id?.slice(0, 16) ?? "none"} tone={lane.permission_snapshot_id ? "info" : "warn"} />
          </InspectorSection>
        </aside>

        <main className="strategy-chart-stage">
          <Suspense fallback={<div className="elite-card chart-loading">Loading canonical tape…</div>}><ScannerChart /></Suspense>
        </main>
      </div>

      <div className="strategy-bottom-grid">
        <EvidenceTape lane={lane} events={lifecycleEvents} />
        <section className="elite-card performance-card">
          <div className="elite-card__header"><div><span className="eyebrow">Operational book</span><h3>Kernel performance</h3></div><TerminalBadge tone="neutral">kernel_v1 only</TerminalBadge></div>
          <div className="performance-card__metrics">
            <div><span>Resolved</span><strong>{lane.lifecycle.resolved}</strong></div>
            <div><span>Booked net</span><strong className={(lane.lifecycle.net_value ?? 0) < 0 ? "text-short" : "text-long"}>{lane.lifecycle.net_value == null ? "—" : `$${lane.lifecycle.net_value.toFixed(2)}`}</strong></div>
            <div><span>Booked / wall / approve</span><strong>{lane.execution_cost_bps?.toFixed(1) ?? "—"} / {lane.gate_cost_bps?.toFixed(1) ?? "—"} / {lane.approval_gross_floor_bps?.toFixed(1) ?? "—"} bps</strong></div>
            <div><span>Entry clock</span><strong>{text(lane.runtime_contract?.entry_clock)}</strong></div>
          </div>
          {lane.lifecycle.resolved < 20 && <div className="sample-warning"><span>UNDER-SAMPLED</span><p>Performance exists for audit, but the population is too small for a promotion claim.</p></div>}
        </section>
      </div>
    </div>
  );
}
