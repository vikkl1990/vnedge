import { useEffect, useState } from "react";

export interface MarketStage {
  version: string; spec_hash: string; timeframe: string; state: string; stage: string;
  previous_stage: string | null; stage_id: string | null; as_of: string | null;
  since: string | null; bars_in_state: number; supports: string[]; conflicts: string[];
  issues: string[]; transitions: { from: string; to: string; at: string }[];
  watch: { toward: string; condition: string; level: number }[]; invalidation?: string;
}
export interface FundamentalField {
  metric: string; label: string; status: string; value: number | null; reason: string;
  unit?: string; period_end?: string; received_at?: string; max_age_seconds?: number;
  evidence_id?: string; source_url: string | null; methodology?: string;
}
export interface Fundamentals {
  version: string; template: string; health: string; fields: FundamentalField[]; note: string;
}
const label = (text: string | null | undefined) => (text ?? "unknown").replace(/_/g, " ");
const time = (text: string | null | undefined) => text ? new Date(text).toLocaleString("en-GB", {timeZone:"UTC", hour12:false}) + " UTC" : "Not available";

export function AnalystContext({ stages = [], fundamentals }: { stages?: MarketStage[]; fundamentals?: Fundamentals }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  return <div className="ca-context">
    <h4>Market development</h4>
    <p className="ca-muted">Daily / 4h context → separate setup clocks. No entry trigger or automatic activation.</p>
    <div className="ca-stage-grid">{stages.map(stage => {
      const age = Math.max(now, Date.now()) - Date.parse(stage.as_of ?? "");
      const current = stage.state === "current" && age >= 0 && age <= (stage.timeframe === "1d" ? 86400 : 14400) * 1500;
      return <article className="ca-stage-card" key={stage.timeframe}>
        <div className="ca-between"><strong>{stage.timeframe}</strong><span className="ca-chip">{current ? label(stage.stage) : stage.state === "current" ? "stale" : stage.state}</span></div>
        {!current ? <p>No current stage conclusion. {stage.issues.map(label).join(" · ")}</p> : <>
          <dl className="ca-levels"><div><dt>Previous stage</dt><dd>{label(stage.previous_stage)}</dd></div><div><dt>Observed time in state</dt><dd>{stage.bars_in_state} closed bars</dd></div></dl>
          <small>Confirmed {time(stage.since)} · as of {time(stage.as_of)}</small>
          <p className="ca-muted">Bounded history: duration may be a lower bound. Markets need not follow a fixed cycle.</p>
          <h5>Supporting measurements</h5><ul>{stage.supports.map(s => <li key={s}>{s}</li>)}</ul>
          <h5>Conflicts and gaps</h5><ul>{[...stage.conflicts, ...stage.issues].map(s => <li key={s}>{label(s)}</li>)}</ul>
          <h5>What would confirm a change?</h5>{stage.watch.map(w => <p key={w.toward}><strong>{label(w.toward)}:</strong> {w.condition}</p>)}
          <p>{stage.invalidation}</p>
          <details><summary>Stage memory · {stage.transitions.length} observed changes</summary>{stage.transitions.map((event, i) => <p key={i}>{time(event.at)}: {label(event.from)} → {label(event.to)}</p>)}</details>
        </>}
        <details><summary>Version and evidence</summary><p>{stage.version}</p><code>{stage.spec_hash}</code>{stage.stage_id && <code>{stage.stage_id}</code>}</details>
      </article>;
    })}</div>
    {!stages.length && <p>Stage evidence has not arrived.</p>}
    <p className="ca-warning">Accumulation and distribution are hypotheses—not proof of institutional activity.</p>
    <h4>Crypto fundamentals</h4>
    <p className="ca-muted">Template: {label(fundamentals?.template)} · fundamental health: {label(fundamentals?.health)}. No blended confidence score.</p>
    <div className="ca-fundamental-grid">{fundamentals?.fields.map(field => {
      const clock = Math.max(now, Date.now()), end = Date.parse(field.period_end ?? ""), received = Date.parse(field.received_at ?? "");
      const ttl = (field.max_age_seconds ?? 0)*1000;
      const current = field.status === "current" && end <= received && received <= clock && clock-end <= ttl && clock-received <= ttl;
      const safeSource = field.source_url?.startsWith("https://api.llama.fi/") ? field.source_url : null;
      return <article key={field.metric}><h5>{field.label}</h5>
        <strong>{current && field.value != null ? `${field.value.toLocaleString("en-US", {maximumFractionDigits:2})} ${field.unit ?? ""}` : label(field.status === "current" ? "stale" : field.status)}</strong>
        <p className="ca-muted">{field.reason}</p>
        {field.period_end && <small>Period ending {time(field.period_end)}<br />Retrieved {time(field.received_at)}</small>}
        {safeSource && <p><a href={safeSource} target="_blank" rel="noreferrer">Source observation ↗</a></p>}
        {field.evidence_id && <details><summary>Definition and evidence</summary><p>{field.methodology}</p><code>{field.evidence_id}</code></details>}
      </article>;
    })}</div>
    <p className="ca-muted">{fundamentals?.note ?? "No fundamental observations available."}</p>
    <p className="ca-muted">Funding, open interest and spread remain in Public market conditions—not business fundamentals. Return improvement has not been established.</p>
  </div>;
}
