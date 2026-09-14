import { useMemo } from "react";
import { useLanes, useMeta, useRiskSnapshot } from "../queries";

type GateKey = "data_ready" | "decision_ready" | "parity_ready" | "execution_ready" | "live_ready";

const GATES: Array<[GateKey, string, string]> = [
  ["data_ready", "Data", "canonical bars"],
  ["decision_ready", "Decision", "closed-bar path"],
  ["parity_ready", "Parity", "matched evidence"],
  ["execution_ready", "Execution", "kernel only"],
  ["live_ready", "Live", "capital authority"],
];

function aggregateGate(lanes: NonNullable<ReturnType<typeof useLanes>["data"]>["lanes"], key: GateKey) {
  const operational = lanes.filter((lane) => lane.observation_class === "shadow_observe");
  if (!operational.length) return { state: "unknown" as const, count: 0, total: 0 };
  const reported = operational.filter((lane) => lane.runtime_readiness != null);
  if (!reported.length) return { state: "unknown" as const, count: 0, total: operational.length };
  const count = reported.filter((lane) => lane.runtime_readiness?.[key] === true).length;
  return { state: count === operational.length ? "ready" as const : "blocked" as const, count, total: operational.length };
}

export function firstBlocker(lanes: NonNullable<ReturnType<typeof useLanes>["data"]>["lanes"]) {
  if (!lanes.some(lane => lane.observation_class === "shadow_observe" && lane.runtime_readiness)) return "operational readiness unknown";
  const keys = ["live_blockers", "execution_blockers", "parity_blockers", "decision_blockers", "data_blockers"] as const;
  for (const lane of lanes.filter((item) => item.observation_class === "shadow_observe")) {
    for (const key of keys) {
      const blocker = lane.runtime_readiness?.[key]?.[0];
      if (blocker) return `${lane.symbol} · ${blocker.replace(/_/g, " ")}`;
    }
  }
  const operational = lanes.filter(lane => lane.observation_class === "shadow_observe");
  if (operational.some(lane => !lane.runtime_readiness)) return "operational readiness partially reported";
  if (operational.some(lane => GATES.some(([key]) => lane.runtime_readiness?.[key] !== true))) return "readiness blocked or unknown; reason not reported";
  return "no operational blocker reported";
}

export function CockpitCommandBar({ onOpenRisk }: { onOpenRisk: () => void }) {
  const lanes = useLanes();
  const risk = useRiskSnapshot();
  const meta = useMeta();
  const rows = lanes.data?.lanes ?? [];
  const gateState = useMemo(
    () => Object.fromEntries(GATES.map(([key]) => [key, aggregateGate(rows, key)])) as Record<GateKey, ReturnType<typeof aggregateGate>>,
    [rows],
  );
  const transports = [...new Set(rows.map((lane) => lane.decision_transport).filter(Boolean))];
  const sources = [...new Set(rows.map((lane) => lane.candle_source).filter(Boolean))];
  const laneFresh = !lanes.isError && lanes.data?.snapshot_state === "fresh";
  const blocker = laneFresh ? firstBlocker(rows) : "operational snapshot unavailable or stale";
  const riskKnown = !risk.isError && !!risk.data && laneFresh;
  const journalOk = riskKnown && risk.data?.journal.available === true && risk.data.journal.recovery_degraded === false;
  const snapshotStale = lanes.data?.snapshot_state !== "fresh";

  return (
    <section className="command-deck" aria-label="Runtime authority and readiness">
      <div className="command-deck__gates">
        {GATES.map(([key, label, detail]) => {
          const gate = laneFresh ? gateState[key] : { state: "unknown", count: 0, total: 0 };
          return (
            <div key={key} className={`readiness-cell readiness-cell--${gate.state}`}>
              <span className="readiness-cell__orb" aria-hidden="true" />
              <div>
                <div className="readiness-cell__label">{label}</div>
                <div className="readiness-cell__detail">{detail}</div>
              </div>
              <span className="readiness-cell__value">
                {gate.state === "unknown" ? "—" : gate.state === "ready" ? "PASS" : `${gate.count}/${gate.total}`}
              </span>
            </div>
          );
        })}
      </div>

      <div className="command-deck__rail">
        <div className="command-deck__blocker">
          <span className="eyebrow">Primary blocker</span>
          <strong title={blocker}>{blocker}</strong>
        </div>
        <div className="command-deck__facts">
          <span><i className={snapshotStale ? "dot dot--warn" : "dot dot--muted"} />snapshot {lanes.data?.snapshot_state ?? "unknown"}</span>
          <span><i className={transports.includes("router") ? "dot dot--pass" : "dot dot--warn"} />{transports.join(" + ") || "transport unknown"}</span>
          <span><i className={journalOk ? "dot dot--pass" : "dot dot--fail"} />journal {journalOk ? "sealed" : "blocked"}</span>
          <span><i className={risk.data?.kill.active ? "dot dot--fail" : "dot dot--muted"} />kill {!riskKnown ? "unknown" : risk.data?.kill.active ? "active" : "clear"}</span>
          <span><i className={risk.data?.daily_halt.active ? "dot dot--fail" : "dot dot--muted"} />halt {!riskKnown ? "unknown" : risk.data?.daily_halt.active ? "active" : "clear"}</span>
          <span><i className={riskKnown && risk.data?.reconciliation.clean ? "dot dot--pass" : "dot dot--warn"} />recon {riskKnown ? risk.data?.reconciliation.status ?? "unknown" : "unknown"}</span>
          <span className="hidden 2xl:inline"><i className="dot dot--warn" />private {risk.data?.live.delta_private_status ?? "unknown"}</span>
          <span className="hidden 2xl:inline" title={sources.join(" + ")}>source {sources[0] ?? "unknown"}</span>
          <span className="hidden 2xl:inline">{meta.data?.build_sha?.slice(0, 8) ?? "—"}</span>
        </div>
        <button type="button" className="risk-drawer-trigger" onClick={onOpenRisk}>
          <span>Risk console</span>
          <span aria-hidden="true">↗</span>
        </button>
      </div>
    </section>
  );
}
