import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../api";

export interface StageOutcomesReport {
  state: string; generated_at?: string; activated_at?: string;
  contexts: { row_key: string; strategy_id: string; cutoff: string; decision_id: string | null;
    stages: { timeframe: string; stage: string; state: string; issues?: string[]; report_id?: string }[] }[];
  groups: { group_id: string; lane: string; strategy_id: string; symbol: string; timeframe: string;
    entry_clock: string; mode: string; cost_profile_id: string; cost_config_sha256: string;
    label_contract: string; stage_tf: string; stage: string; stage_version: string;
    n: number; state: string; mean_net_bps: number; net_usd: number; profit_factor: number | null }[];
  audits: { lane: string; state: string; issues: Record<string, number> }[];
  sources?: { lane: string; state: string; caught_up: boolean; invalid_records: number }[];
  counts?: Record<string, number>; contexts_truncated?: boolean; lanes_truncated?: boolean;
  directory_available?: boolean;
}

export function StageOutcomesFacts({ data, failed = false }: { data?: StageOutcomesReport; failed?: boolean }) {
  const age = Date.now() - Date.parse(data?.generated_at ?? "");
  const current = !failed && data?.state === "current" && age >= 0 && age <= 900_000;
  return <div aria-label="Scanner outcomes by market stage">
    <p>Official Delta stage context recorded beside scanner evaluations. No signal filters, scores or trading permissions changed.</p>
    <p className="ca-muted">Only context available by the decision-bar close is eligible. Paper results include verified fees and settled funding—not virtual shadow profits.</p>
    {!current ? <p role="status" className="ca-warning">{failed ? "Evidence connection unavailable" : data?.state === "worker_not_started" ? "Stage recorder has not started" : data ? "Stage report unavailable or stale" : "Reading stage evidence…"}. No current outcome conclusion.</p> : <>
      <p>{data.contexts.length} captured records for this symbol in the displayed journal window · started {data.activated_at} · updated {data.generated_at}</p>
      <p className="ca-muted">Bounded recent evaluations, not complete history. Counts below are observations, not an OOS test. A decision and its diagnostic evaluation may both be present.</p>
      {(data.contexts_truncated || data.lanes_truncated || data.directory_available === false) && <p className="ca-warning">Coverage is incomplete: a source directory is missing or the processing/display limit was reached.</p>}
      {!data.groups.length && <p>Awaiting verified paper outcomes with a forward stage binding. Evaluations and missing labels are not zero-return trades.</p>}
      {data.groups.map(g => <article className="ca-evidence-ref" key={g.group_id}>
        <strong>{g.strategy_id} · {g.stage_tf} {g.stage.replace(/_/g, " ")}</strong>
        <p>{g.symbol} · {g.timeframe} · {g.entry_clock} · {g.mode} · {g.cost_profile_id}</p>
        <p>{g.n} outcomes · {g.state} · mean net {g.mean_net_bps.toFixed(2)} bps · net ${g.net_usd.toFixed(2)} · PF {g.profit_factor == null ? "not reported" : g.profit_factor.toFixed(2)}</p>
        <small>{g.stage_version} · {g.label_contract} · cost hash {g.cost_config_sha256}</small>
      </article>)}
      <p className="ca-muted">Below 30 outcomes: INSUFFICIENT. Larger samples remain descriptive; no edge or promotion is established. “All bound outcomes” is the comparison baseline; do not add the 4h and 1d groups together.</p>
      <details><summary>Recent context captures</summary>{data.contexts.slice(0, 30).map(c => <div className="ca-history-row" key={c.row_key}>
        <strong>{c.strategy_id}</strong><p>{c.cutoff} · {c.decision_id ? "bound decision" : "diagnostic evaluation, not a trade"}</p>
        {c.stages.map(s => <p key={s.timeframe}>{s.timeframe}: {s.state === "current" ? s.stage.replace(/_/g, " ") : "context unavailable at decision"} · {s.issues?.join(" · ")} {s.report_id && <code>{s.report_id.slice(0, 12)}</code>}</p>)}
      </div>)}</details>
      <details><summary>Fleet processing and accounting gaps</summary>
        <p>Fleet-wide processing counts (not symbol totals): {Object.entries(data.counts ?? {}).map(([k, v]) => `${k}: ${v}`).join(" · ")}</p>
        {data.audits.map(a => <p key={a.lane}>{a.lane}: {a.state} · {Object.entries(a.issues).map(([k, v]) => `${k}: ${v}`).join(" · ")}</p>)}
        {data.sources?.filter(s => !s.caught_up || s.invalid_records || s.state !== "ok").map(s => <p className="ca-warning" key={s.lane}>{s.lane}: {s.state} · caught up {String(s.caught_up)} · malformed records {s.invalid_records}</p>)}
      </details>
    </>}
  </div>;
}

export function AnalystStageOutcomes({ exchange, symbol }: { exchange: string; symbol: string }) {
  const query = useQuery({ queryKey: ["analyst-stage-outcomes", exchange, symbol],
    queryFn: () => apiGet<StageOutcomesReport>(`/api/crypto-analyst/stage-outcomes/${encodeURIComponent(symbol)}?${new URLSearchParams({ exchange })}`),
    refetchInterval: 30_000, retry: 1 });
  return <StageOutcomesFacts data={query.data} failed={query.isError} />;
}
