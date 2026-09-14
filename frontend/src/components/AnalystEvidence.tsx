import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../api";
import type { AnalystMarket } from "./CryptoAnalyst";
import { AnalystContext, type MarketStage, type Fundamentals } from "./AnalystContext";

export interface PublicObservation {
  state: string; source?: string; evidence_id?: string; venue_ts?: string;
  received_at?: string; issues?: string[]; values?: Record<string, number | null>;
  sample_trades?: number; buy_share_pct?: number;
  expires_after_s?: number;
}
interface EvidenceRef { id: string; kind: string; as_of: string | null; state: string; source: string; summary: string }
export interface Dossier {
  dossier_id: string; generated_at: string; symbol: string; frames: AnalystMarket[];
  market_evidence: Record<string, PublicObservation>; gaps: string[]; conflicts: string[];
  clock_note: string; evidence: EvidenceRef[];
  stages?: MarketStage[]; fundamentals?: Fundamentals;
}
interface Answer {
  answer_hash: string; generated_at: string; answer: { text: string; citations: string[] }[];
  evidence: EvidenceRef[]; disclaimer: string;
}
interface History {
  status: string;
  reports: { evidence_id: string; available_at: string; body: { frames: Record<string, { bias: string; alignment: number }> } }[];
  changes: { evidence_id: string; available_at: string; body: { event: string } }[];
  stage_reports?: { evidence_id: string; available_at: string; body: { stages: MarketStage[] } }[];
}
function n(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "Unavailable" : value.toLocaleString("en-US", { maximumFractionDigits: 4 });
}
function when(value: string | undefined | null): string {
  return value ? new Date(value).toLocaleString("en-GB", { timeZone: "UTC", hour12: false }) + " UTC" : "No source time";
}

export function observationState(observation: PublicObservation | undefined, now = Date.now()): string {
  if (observation?.state !== "current") return observation?.state ?? "unavailable";
  const venue = Date.parse(observation.venue_ts ?? ""), receipt = Date.parse(observation.received_at ?? "");
  const ttl = observation.expires_after_s;
  if (!Number.isFinite(venue) || !Number.isFinite(receipt) || ttl == null || !Number.isFinite(ttl) || ttl <= 0) return "unavailable";
  return venue <= receipt && receipt <= now && now - venue <= ttl * 1000 && now - receipt <= ttl * 1000 ? "current" : "stale";
}

export function MarketConditions({ observations }: { observations?: Record<string, PublicObservation> }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const conditions = observations?.conditions, flow = observations?.flow;
  const conditionState = observationState(conditions, Math.max(now, Date.now())), flowState = observationState(flow, Math.max(now, Date.now()));
  const values = conditionState === "current" ? conditions?.values : undefined;
  return <section className="ca-public" aria-label="Public market conditions">
    <div className="ca-between"><h4>Public market conditions</h4><span className="ca-chip">{conditionState}</span></div>
    <p className="ca-muted">Independent REST sample · not lane BBO or trade permission</p>
    <dl className="ca-levels">
      <div><dt>Public mark price</dt><dd>{n(values?.mark_price)}</dd></div>
      <div><dt>Sample spread (bps)</dt><dd>{n(values?.spread_bps)}</dd></div>
      <div><dt>Open interest (contracts)</dt><dd>{n(values?.open_interest_contracts)}</dd></div>
      <div><dt>Indicative funding (%)</dt><dd>{n(values?.indicative_funding_pct)}</dd></div>
      <div><dt>Aggressor-buy sample (%)</dt><dd>{n(flowState === "current" ? flow?.buy_share_pct : undefined)}</dd></div>
    </dl>
    <p className="ca-muted">{conditions?.source ?? "No public source"} · {when(conditions?.venue_ts)} · Funding is not settled cash.</p>
    <p className="ca-muted">Flow: {flowState}{flowState === "current" ? ` · ${flow?.sample_trades} prints; incomplete coverage` : ""}. No institution identity inferred.</p>
    {(conditions?.issues ?? []).map(issue => <p className="ca-warning" key={issue}>{issue.replace(/_/g, " ")}</p>)}
  </section>;
}

export function DossierFacts({ data }: { data?: Dossier }) {
  if (!data) return <p className="ca-muted">Awaiting timestamped evidence.</p>;
  return <>
    <div className="ca-tf-grid">{data.frames.map(frame => <article key={frame.timeframe}>
      <div className="ca-between"><strong>{frame.timeframe}</strong><span className={`ca-chip ca-${frame.state === "current" ? frame.bias : "muted"}`}>{frame.state === "current" ? frame.bias : frame.state}</span></div>
      <p>{frame.state === "current" ? `${frame.alignment} / ±100 alignment` : "No current directional conclusion"}</p><small>{when(frame.as_of)}</small>
      <p className="ca-muted">{frame.source === "official_delta_ohlc" ? "Official Delta history · " : ""}{frame.issues.map(i => i.replace(/_/g, " ")).join(" · ") || "Closed canonical evidence"}</p>
    </article>)}</div>
    <p className="ca-muted">{data.clock_note}</p>
    {data.conflicts.map(c => <p className="ca-warning" key={c}>↔ {c.replace(/_/g, " ")}</p>)}
    <MarketConditions observations={data.market_evidence} />
    <details><summary>Evidence ledger · {data.evidence.length} references</summary>
      {data.evidence.map(e => <div className="ca-evidence-ref" key={e.id}><strong>{e.summary}</strong><p>{e.source} · {when(e.as_of)}</p><code>{e.id}</code></div>)}
      {!data.evidence.length && <p>No supported market observations yet.</p>}
    </details>
  </>;
}

export function AnalystEvidence({ exchange, symbol, source = "canonical" }: { exchange: string; symbol: string; source?: string }) {
  const [tab, setTab] = useState(source === "official_delta" ? "dossier" : "context"), [question, setQuestion] = useState(""), [asked, setAsked] = useState("");
  const scope = new URLSearchParams({ exchange, source });
  const dossier = useQuery({ queryKey: ["analyst-dossier", exchange, symbol, source],
    queryFn: () => apiGet<Dossier>(`/api/crypto-analyst/symbol/${encodeURIComponent(symbol)}?${scope}`), refetchInterval: 30_000, retry: 1 });
  const history = useQuery({ queryKey: ["analyst-history", exchange, symbol, source], enabled: tab === "history",
    queryFn: () => apiGet<History>(`/api/crypto-analyst/history/${encodeURIComponent(symbol)}?${scope}`), refetchInterval: 60_000, retry: 1 });
  const answer = useQuery({ queryKey: ["analyst-answer", exchange, symbol, source, asked], enabled: !!asked && tab === "ask",
    queryFn: () => apiGet<Answer>(`/api/crypto-analyst/answer/${encodeURIComponent(symbol)}?${new URLSearchParams({ exchange, source, question: asked })}`), retry: 1, staleTime: 0 });
  return <section className="ca-evidence-panel" aria-label="Multi-timeframe analyst">
    <div className="ca-between"><h3>Explore {symbol}</h3><span className="ca-overline">EVIDENCE FIRST</span></div>
    <div className="ca-evidence-tabs" role="tablist" aria-label="Evidence views">
      {[["context", "Stage & fundamentals"], ["dossier", "Multi-timeframe"], ["ask", "Ask the evidence"], ["history", "History & changes"]].map(([id, name]) => <button role="tab" aria-selected={tab === id} key={id} onClick={() => setTab(id)}>{name}</button>)}
    </div>
    {dossier.isError && <p className="ca-warning" role="alert">Evidence connection unavailable. Any previous dossier is historical.</p>}
    {tab === "dossier" && <DossierFacts data={dossier.data} />}
    {tab === "context" && <AnalystContext stages={dossier.data?.stages} fundamentals={dossier.data?.fundamentals} />}
    {tab === "ask" && <div className="ca-ask">
      <p>Local evidence answers · no paid AI model · no orders</p>
      <div className="ca-question-presets">{["What stage is this market in?", "Show fundamentals, revenue and emissions", "What supports or contradicts the trend?", "Show VWAP and reference levels", "What do funding, open interest and flow show?", "What evidence is missing?"].map(q => <button key={q} onClick={() => { setQuestion(q); setAsked(q); }}>{q}</button>)}</div>
      <form onSubmit={e => { e.preventDefault(); if (question.trim()) { if (asked === question.trim()) void answer.refetch(); else setAsked(question.trim()); } }}>
        <label htmlFor="analyst-question">Ask about this market’s evidence</label>
        <input id="analyst-question" maxLength={500} value={question} onChange={e => setQuestion(e.target.value)} placeholder="Why are the timeframes disagreeing?" />
        <small>Market questions only; do not enter personal or account information.</small>
        <button type="submit" disabled={!question.trim() || answer.isFetching}>{answer.isFetching ? "Reading evidence…" : "Explain"}</button>
      </form>
      {answer.isError && <p role="alert" className="ca-warning">Could not retrieve an evidence answer. No conclusion is available.</p>}
      {answer.data && !answer.isError && <div className="ca-answer"><small>Answered {when(answer.data.generated_at)} · snapshot, not a streaming recommendation</small>
        {answer.data.answer.map((line, i) => <p key={i}>{line.text}{!!line.citations.length && <small> Sources: {line.citations.map(ref => <code key={ref} title={ref}>{ref.slice(0, 10)} </code>)}</small>}</p>)}
        <p className="ca-muted">{answer.data.disclaimer}</p>
        <details><summary>Answer audit trail</summary><code>{answer.data.answer_hash}</code>{answer.data.evidence.map(e => <p key={e.id}>{e.summary}<br /><code>{e.id}</code></p>)}</details>
      </div>}
    </div>}
    {tab === "history" && <div>
      <p className="ca-muted">Saved closed-bar reports and profile changes. Baselines are not alerts or fills.</p>
      {(history.isError || history.data?.status === "evidence_read_failed") && <p role="alert" className="ca-warning">History unavailable; cannot confirm the saved record.</p>}
      {history.isPending && <p>Reading saved evidence…</p>}
      {!history.isError && history.data?.status === "no_saved_reports" && <p>No saved reports yet. The opt-in Analyst worker must run with eligible canonical data.</p>}
      {history.data?.changes.map(e => <div className="ca-history-row" key={e.evidence_id}><strong>{e.body.event.replace(/_/g, " ")}</strong><small>{when(e.available_at)}</small></div>)}
      {history.data?.reports.map(r => <details key={r.evidence_id}><summary>{when(r.available_at)} · saved report</summary><p>{Object.entries(r.body.frames).map(([tf, f]) => `${tf}: ${f.bias} (${f.alignment})`).join(" · ")}</p><code>{r.evidence_id}</code></details>)}
      {history.data?.stage_reports?.map(r => <details key={r.evidence_id}><summary>{when(r.available_at)} · market-stage report</summary><p>{r.body.stages.map(s => `${s.timeframe}: ${s.stage.replace(/_/g, " ")}`).join(" · ")}</p><code>{r.evidence_id}</code></details>)}
    </div>}
  </section>;
}
