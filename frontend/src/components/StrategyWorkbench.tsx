import { Suspense, lazy, useEffect, useMemo, useState } from "react";
import type { CorrectionLane, ScannerAuditEvent } from "../api";
import { useCostModel, useJournal, useLanes } from "../queries";
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
      <strong className={`fact-tone fact-tone--${tone}`}>{value}</strong>
    </div>
  );
}

function Funnel({ lane }: { lane: CorrectionLane }) {
  const items = [
    ["Eval", lane.funnel.evals ?? 0],
    ["Ready", lane.funnel.ready ?? 0],
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
  const costs = useCostModel();
  const lanes = useMemo(() => (lanesQuery.data?.lanes ?? []).filter((lane) => lane.observation_class === "shadow_observe"), [lanesQuery.data]);
  const [selectedId, setSelectedId] = useState("");
  const lane = lanes.find((item) => item.lane_id === selectedId) ?? lanes[0] ?? null;
  useEffect(() => {
    if (!selectedId && lanes[0]) setSelectedId(lanes[0].lane_id);
  }, [lanes, selectedId]);
  const evaluation = lane?.last_eval ?? {};
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
          <div><span>Cost</span><strong>{lane.cost_profile} · {lane.round_trip_bps?.toFixed(1) ?? costs.data?.taker_round_trip_cost_bps?.toFixed(1) ?? "—"} bps</strong></div>
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
            <Fact label="Ready" value={text(evaluation.mreg_ready, text(lane.drought?.mreg_ready))} tone={booleanTone(evaluation.mreg_ready ?? lane.drought?.mreg_ready)} />
            <Fact label="State" value={text(evaluation.mreg_state, "flat / unknown")} tone={evaluation.mreg_state === "continuation" ? "info" : "neutral"} />
            <Fact label="EMA 200" value={text(evaluation.mreg_ema200_ready, "not reported")} tone={booleanTone(evaluation.mreg_ema200_ready)} />
            <Fact label="Daily bars" value={text(evaluation.mreg_daily_observations, "—")} />
            <Fact label="EMA · MACD · RSI" value={`${text(evaluation.mreg_ema_state, "—")} · ${text(evaluation.mreg_macd_impulse, "—")} · ${text(evaluation.mreg_rsi_zone, "—")}`} />
            <div className="context-age-grid">{["4h", "1d", "1w"].map((tf) => <div key={tf}><span>{tf}</span><strong>{age(contextAges[tf])}</strong></div>)}</div>
          </InspectorSection>

          <InspectorSection title="Structure" kicker="geometry">
            <Fact label="Ready" value={text(evaluation.bos15_structure_ready, text(lane.drought?.structure_ready))} tone={booleanTone(evaluation.bos15_structure_ready ?? lane.drought?.structure_ready)} />
            <Fact label="1h trend" value={text(evaluation.bos15_structure_trend)} />
            <Fact label="4h trend" value={text(evaluation.bos15_htf_structure_trend)} />
            <Fact label="Parent identity" value={text(evaluation.bos15_parent_identity_ok)} tone={booleanTone(evaluation.bos15_parent_identity_ok)} />
            <Fact label="AVWAP" value={text(evaluation.mreg_avwap_source, text(evaluation.bos15_dual_avwap_bias, "unused"))} />
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
            <div><span>Gate wall</span><strong>{lane.round_trip_bps?.toFixed(1) ?? "—"} bps</strong></div>
            <div><span>Entry clock</span><strong>{text(lane.runtime_contract?.entry_clock)}</strong></div>
          </div>
          {lane.lifecycle.resolved < 20 && <div className="sample-warning"><span>UNDER-SAMPLED</span><p>Performance exists for audit, but the population is too small for a promotion claim.</p></div>}
        </section>
      </div>
    </div>
  );
}
