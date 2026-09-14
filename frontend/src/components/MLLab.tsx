import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../api";
import { TerminalBadge, TerminalPanel } from "./Terminal";

export interface MLLabPayload {
  pipeline?: {
    schema: string; plans_total: number; datasets_total: number; runs_total: number; predictions_total: number;
    ledger_bound_paper_labels?: number; ledger_coverage_complete?: boolean; ledger_exclusions?: Record<string, number>;
    ledger_sources?: {lane: string; state: string; labels: number; rejections: Record<string, number>}[];
    non_label_sources?: {file: string; reason: string}[];
    readiness_worklist?: {id: string; title: string; status: string; owner: string; action: string}[];
    funding_evidence?: { stale: boolean; markets: {symbol: string; history_archived: boolean; settlement_verified: boolean; reason: string}[] };
    family_results?: {family_id: string; statistics: {raw_trials: number; calendar_days: number; pbo: number; dsr_by_trial: number[]}}[];
    failed_attempts: number; incomplete_attempts: number;
    datasets: { dataset_id: string; rows: number; cohort: Record<string, string>; rejections: Record<string, number> }[];
    runs: { plan_id: string; dataset_id: string; purpose: string; status: string; model_hash: string;
      calibration: string; split_rows: Record<string, number>; purged_rows: number;
      development_validation?: {unique_decisions: number; folds: unknown[]} | null;
      metrics: { auc: number | null; brier: number; coverage: number;
        baseline: { n: number; mean_net_bps: number | null }; selected: { n: number; mean_net_bps: number | null } } }[];
    predictions: { decision_id: string; plan_id: string; prediction_at: string; probability: number }[];
    feedback?: { decision_id: string; probability: number; target: number; net_bps: number }[];
    errors: { file: string; reason: string }[];
  } | null;
  artifact_state: string; worker_generated_at: string | null; source_as_of: string | null;
  worker_age_s: number | null; source_age_s: number | null;
  audit: {
    evidence_hash: string; counts: Record<string, number>; operational_labels: number;
    label_status: string; history_complete: boolean; files_truncated: boolean;
    source_directory_available: boolean;
    sources: { file: string; state: string; truncated: boolean; invalid: number; incomplete_tail: boolean }[];
    exclusions: Record<string, number>;
    cohorts: { strategy_id: string; exchange: string; symbol: string; timeframe: string;
      feature_fingerprint: string; rows: number; fired_rows: number; complete_feature_rows: number;
      missing_values: number; operational_labels: number; trainable: boolean }[];
    feature_missingness: { feature: string; missing_rows: number }[];
    training: { status: string; min_labels: number; cpcv_min_labels: number; min_train_fold_rows: number; blockers: string[] };
  } | null;
}

const TABS = ["Overview", "Datasets", "Features", "Validation", "Forward feedback"];
const readable = (value: string) => value.replace(/_/g, " ");
export const mlNumber = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString("en-US");
export const mlAge = (seconds: number | null | undefined) => seconds == null ? "unknown" : seconds < 60 ? `${Math.floor(seconds)}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${(seconds / 3600).toFixed(1)}h`;

export function MLLabView({ data, failed = false }: { data?: MLLabPayload; failed?: boolean }) {
  const [tab, setTab] = useState("Overview");
  const audit = data?.audit;
  const c = audit?.counts;
  const pipeline = data?.pipeline;
  const partial = audit && (!audit.source_directory_available || audit.files_truncated || audit.sources.some(s => s.state !== "ok" || s.truncated || s.invalid > 0 || s.incomplete_tail));
  return <TerminalPanel title="ML Lab" meta="MODEL EVIDENCE · REPORT ONLY">
    <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
      <div><div className="eyebrow">From recorded features to defensible labels</div>
        <h2 className="mt-2 text-xl font-semibold">Evidence → dataset → calibrated research model.</h2>
        <p className="mt-2 max-w-3xl text-sm text-dim">Exact decision joins, missing features and rejected outcomes. Research simulations stay separate from operational labels.</p></div>
      <TerminalBadge tone="warn">NO TRADING AUTHORITY</TerminalBadge>
    </div>
    <div className="mb-4 flex flex-wrap gap-3 text-xs font-mono text-dim">
      <TerminalBadge tone={failed || data?.artifact_state !== "CURRENT" ? "warn" : "neutral"}>{failed ? "READ UNAVAILABLE" : data?.artifact_state || "LOADING"}</TerminalBadge>
      <span>Worker report {mlAge(data?.worker_age_s)} old</span><span>Latest source record {mlAge(data?.source_age_s)} old</span>
      <span>Recent bounded audit · not lifetime totals</span>
      <span>Verified experiment artifacts · up to 20 per category</span>
    </div>
    {(failed || (data && !audit)) && <p role="alert" className="mb-4 rounded-lg border border-warn/30 bg-warn/5 p-3 text-sm text-warn">
      {data?.artifact_state === "LEGACY_UNVERIFIED" ? "The status worker still publishes a legacy candle-based dataset. Its labels and validation claims are not admitted here." : "ML audit evidence is unavailable. Missing counts are unknown, not zero."}</p>}
    {partial && <p role="status" className="mb-4 text-xs text-warn">Source coverage is partial. Some files are missing, truncated, malformed or still being written; this audit cannot establish full-history readiness.</p>}
    <nav className="mb-5 flex flex-wrap gap-2" aria-label="ML Lab views">{TABS.map(name =>
      <button key={name} aria-pressed={tab === name} onClick={() => setTab(name)} className={`rounded-md border px-3 py-2 text-xs ${tab === name ? "border-info/50 bg-info/10 text-info" : "border-line text-dim hover:text-txt"}`}>{name}</button>)}</nav>
    {tab === "Overview" && <>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{[
        ["Recorded feature rows", c?.feature_rows], ["Exact feature / decision joins", c?.exact_feature_matches],
        ["Research outcomes · excluded", c?.research_outcomes], ["Ledger-bound paper labels", pipeline?.ledger_bound_paper_labels],
      ].map(([label, value]) => <div key={String(label)} className="rounded-xl border border-line bg-inset/60 p-4"><p className="text-[11px] text-dim">{label}</p><strong className="mt-2 block font-mono text-2xl">{mlNumber(value as number | undefined)}</strong></div>)}</div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <section className="rounded-xl border border-warn/25 bg-warn/5 p-4"><h3 className="text-sm font-semibold">What blocks training</h3>
          {!audit ? <p className="mt-3 text-xs text-dim">Audit unavailable.</p> : <><p className="mt-3 text-sm text-warn">{pipeline ? `${pipeline.ledger_bound_paper_labels ?? 0} paper labels · ${pipeline.plans_total} registered plans` : readable(audit.label_status)}</p>
            <ul className="mt-3 space-y-2 text-xs text-dim">{(pipeline ? Object.keys(pipeline.ledger_exclusions || {}) : audit.training.blockers).map(reason => <li key={reason}>• {readable(reason)}</li>)}</ul>
            {pipeline && !pipeline.plans_total && <p className="mt-3 text-xs text-warn">Register the cohort, feature set and chronological windows before training. No model is started by this dashboard.</p>}
            <p className="mt-4 text-xs">Training floor {audit.training.min_labels} labels per compatible cohort. CPCV floor {audit.training.cpcv_min_labels}; each training fold still needs {audit.training.min_train_fold_rows}. Reaching a count does not prove edge.</p></>}
        </section>
        <section className="rounded-xl border border-line bg-inset/50 p-4"><h3 className="text-sm font-semibold">Evidence pipeline</h3>
          <ol className="mt-3 space-y-3 text-xs">{[["Recorded inputs", audit ? "Audited" : "Unknown"], ["Reconciled paper labels", pipeline ? mlNumber(pipeline.ledger_bound_paper_labels) : "Unknown"],
            ["Registered temporal splits", mlNumber(pipeline?.plans_total)], ["Training + separate calibration", `${mlNumber(pipeline?.runs_total)} completed runs`], ["Untouched judgment", "Human-gated · not promotion"],
            ["Forward reports", mlNumber(pipeline?.predictions_total)]].map(([name, status]) => <li key={name} className="flex justify-between gap-4 border-b border-line/50 pb-2"><span>{name}</span><span className="text-dim">{status}</span></li>)}</ol>
          {pipeline && <p className="mt-3 text-xs text-warn">{pipeline.failed_attempts} failed · {pipeline.incomplete_attempts} incomplete attempts. Ledger history {pipeline.ledger_coverage_complete ? "verified" : "incomplete"}.</p>}
          <p className="mt-3 text-xs text-dim">Each cohort needs at least 200 training, 50 calibration and 50 holdout labels. Counts cannot be pooled across clocks or cost profiles.</p>
          <div className="mt-3 text-xs"><strong>Funding settlement proof</strong><p className="mt-1 text-dim">{pipeline?.funding_evidence?.stale === false ? "Collector report current" : "Collector report unavailable or stale"}</p>{pipeline?.funding_evidence?.markets.map(m => <p key={m.symbol} className="mt-2 text-warn">{m.symbol}: {m.history_archived ? "history archived" : "history unavailable"} · {m.settlement_verified ? "settlement verified" : "settlement unverified"} · {readable(m.reason)}</p>)}</div>
        </section>
      </div>
      <section className="mt-4 rounded-xl border border-line p-4"><h3 className="text-sm font-semibold">What still needs evidence</h3>
        <p className="mt-1 text-xs text-dim">Recorded is not approved. These steps cannot enable trading, start training or manufacture labels.</p>
        <div className="mt-3 grid gap-3 md:grid-cols-2">{pipeline?.readiness_worklist?.map(item => <div key={item.id} className="rounded border border-line bg-inset/50 p-3 text-xs">
          <div className="flex justify-between gap-3"><strong>{item.title}</strong><span className="text-warn">{readable(item.status)}</span></div>
          <p className="mt-2 text-dim">{readable(item.owner)} · {item.action}</p>
        </div>)}</div>
        {!pipeline?.readiness_worklist?.length && <p className="mt-3 text-xs text-dim">Prerequisite report unavailable. No readiness is inferred.</p>}
      </section>
      <section className="mt-4 rounded-xl border border-line p-4"><h3 className="text-sm font-semibold">Accounting lanes</h3>
        {pipeline?.ledger_sources?.map(source => <div key={source.lane} className="mt-3 border-t border-line pt-3 text-xs"><p className="break-all font-mono">{source.lane}</p><p>{source.state} · {source.labels} labels</p><p className="text-warn">{Object.entries(source.rejections).map(([reason, count]) => `${readable(reason)}: ${count}`).join(" · ")}</p></div>)}
        {!pipeline?.ledger_sources?.length && <p className="mt-3 text-xs text-dim">No accounting-lane report available.</p>}
        <p className="mt-3 text-xs text-dim">Non-label infrastructure sources: {pipeline?.non_label_sources?.map(s => `${s.file} (${readable(s.reason)})`).join(" · ") || "none reported"}. These are not missing paper trades.</p>
      </section>
      <section className="mt-4 rounded-xl border border-line p-4"><h3 className="text-sm font-semibold">Exclusion ledger</h3><p className="mt-1 text-xs text-dim">All reasons are counted. One record can fail more than one check.</p>
        <div className="mt-3 grid gap-2 md:grid-cols-2">{Object.entries(audit?.exclusions || {}).sort((a, b) => b[1] - a[1]).map(([reason, n]) => <div key={reason} className="flex justify-between gap-3 rounded bg-inset/60 p-2 text-xs"><span>{readable(reason)}</span><strong className="font-mono text-warn">{n}</strong></div>)}</div>
        {!Object.keys(audit?.exclusions || {}).length && <p className="mt-3 text-xs text-dim">{audit ? "No exclusions in the inspected records. This does not establish label readiness." : "No audit available."}</p>}
      </section>
    </>}
    {tab === "Datasets" && <>
      <p className="mb-3 text-xs text-dim">Frozen datasets bind the entry clock, exact cost configuration, paper book and label contract. The raw feature cohorts below are not themselves trainable datasets.</p>
      {pipeline?.datasets.map(d => <section key={d.dataset_id} className="mb-3 rounded-lg border border-line p-3 text-xs"><strong className="break-all font-mono">{d.dataset_id}</strong><p className="mt-2">{d.rows} admitted rows · {d.cohort.symbol} · {d.cohort.entry_clock} · {d.cohort.cost_profile_id}</p><p className="mt-2 text-dim">{Object.entries(d.rejections).map(([reason, n]) => `${readable(reason)}: ${n}`).join(" · ") || "No excluded rows in this frozen artifact."}</p></section>)}
      <div className="overflow-x-auto"><table className="w-full min-w-[1000px] text-left text-xs"><thead className="text-dim"><tr>{["Strategy", "Exchange / market", "TF", "Feature contract", "Rows", "Fired", "Complete vectors", "Labels"].map(x => <th key={x} className="p-3">{x}</th>)}</tr></thead><tbody>{audit?.cohorts.map(row => <tr className="border-t border-line" key={[row.strategy_id, row.exchange, row.symbol, row.timeframe, row.feature_fingerprint].join("|")}>
        <td className="p-3 break-all">{row.strategy_id}</td><td className="p-3">{row.exchange}<br />{row.symbol}</td><td className="p-3">{row.timeframe}</td><td className="p-3 font-mono">{row.feature_fingerprint}</td><td className="p-3">{row.rows}</td><td className="p-3">{row.fired_rows}</td><td className="p-3">{row.complete_feature_rows}</td><td className="p-3">{row.operational_labels}</td>
      </tr>)}</tbody></table></div>{!audit?.cohorts.length && <p className="p-6 text-center text-sm text-dim">No recorded feature cohorts available.</p>}
    </>}
    {tab === "Features" && <>
      <p className="mb-3 text-xs text-dim">Missing or non-finite values across all audited feature rows. Complete vectors are not proof of live prediction availability.</p>
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">{audit?.feature_missingness.map(row => <div className="flex justify-between gap-3 rounded border border-line p-3 text-xs" key={row.feature}><span className="font-mono">{row.feature}</span><span className={row.missing_rows ? "text-warn" : "text-dim"}>{row.missing_rows} / {c?.feature_rows ?? "—"} missing</span></div>)}</div>
    </>}
    {tab === "Validation" && <section className="rounded-xl border border-line bg-inset/50 p-5">
      <TerminalBadge tone="neutral">{pipeline?.runs.length ? "FROZEN HOLDOUT · RESEARCH ONLY" : "VALIDATION NOT RUN"}</TerminalBadge><h3 className="mt-3 text-lg">Separate calibration. Purged event-time splits.</h3>
      <p className="mt-3 text-sm text-dim">The next model run needs a frozen dataset, label target and horizon, chronological training/calibration/test windows, event-time purge, baseline comparison and the exact cost profile. Existing research models are not silently imported.</p>
      {!pipeline?.runs.length && <p className="mt-4 text-sm text-dim">No completed controlled run is available. Metrics remain unknown, not zero.</p>}
      {pipeline?.runs.filter(r => r.development_validation).map(r => <p key={`cpcv:${r.plan_id}`} className="mt-3 text-xs">Development CPCV: {r.development_validation!.folds.length} folds · {r.development_validation!.unique_decisions} unique decisions. Not untouched judgment.</p>)}
      {pipeline?.family_results?.map(r => <p key={r.family_id} className="mt-3 text-xs">Registered family: {r.statistics.raw_trials} trials · {r.statistics.calendar_days} days · PBO {r.statistics.pbo.toFixed(3)} · DSR {r.statistics.dsr_by_trial.map(x => x.toFixed(3)).join(" / ")}. Unweighted net-bps evidence, not portfolio returns or promotion.</p>)}
      {pipeline?.runs.map(run => <div key={run.plan_id} className="mt-4 border-t border-line pt-4 text-xs"><p className="break-all font-mono">{run.plan_id}</p><p className="mt-2">{run.purpose} · {run.status} · train {run.split_rows.train} / calibration {run.split_rows.calibration} / test {run.split_rows.test} · purged {run.purged_rows}</p><div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-4">{[["OOS AUC", run.metrics.auc], ["Brier", run.metrics.brier], ["Baseline net bps/trade", run.metrics.baseline.mean_net_bps], ["Selected net bps/trade", run.metrics.selected.mean_net_bps]].map(([name, value]) => <div key={String(name)}><p className="text-dim">{name}</p><strong>{value == null ? "—" : Number(value).toFixed(3)}</strong></div>)}</div><p className="mt-3">Selected {run.metrics.selected.n}/{run.metrics.baseline.n} · coverage {(run.metrics.coverage * 100).toFixed(1)}%. Not a portfolio equity curve or a promotion verdict.</p></div>)}
      <p className="mt-5 text-xs text-warn">No public training or promotion controls. Status refresh never starts a training run.</p>
    </section>}
    {tab === "Forward feedback" && <section className="rounded-xl border border-line p-5"><h3 className="text-lg">Prediction before outcome. Outcome after fills.</h3>
      <p className="mt-3 text-sm text-dim">Background feature logging is not a historical live prediction. Forward feedback requires a frozen model hash, feature snapshot and prediction timestamp, then a mature reconciled outcome on the same decision ID.</p>
      <p className="mt-4 text-xs text-warn">Reports are post-ARM observations, never entry approvals. Only predictions recorded before a reconciled exit qualify for feedback.</p>
      {!pipeline?.predictions.length && <p className="mt-3 text-sm text-dim">No forward reports recorded. A frozen model and an explicitly started report worker are required.</p>}
      {pipeline?.predictions.map(p => <div key={`${p.plan_id}:${p.decision_id}`} className="mt-3 border-t border-line pt-3 text-xs"><p className="break-all font-mono">{p.decision_id}</p><p>{p.prediction_at} · {(p.probability * 100).toFixed(1)}% net-positive estimate · report only</p></div>)}
      <p className="mt-4 text-xs text-dim">Mature matched feedback in this page: {pipeline?.feedback?.length ?? 0}. Retrospective test predictions are excluded.</p>
    </section>}
    {!!pipeline?.errors.length && <p role="alert" className="mt-4 text-xs text-warn">Pipeline artifact errors: {pipeline.errors.map(e => `${e.file}: ${e.reason}`).join(" · ")}</p>}
    <details className="mt-5 text-xs text-dim"><summary className="cursor-pointer">Source coverage and evidence fingerprint</summary>
      <p className="my-3 break-all font-mono">{audit?.evidence_hash || "No audit fingerprint"}</p>
      {audit?.sources.map(s => <div key={s.file} className="flex flex-wrap justify-between gap-2 border-t border-line py-2"><span className="break-all">{s.file}</span><span>{s.state} · {s.invalid} invalid{s.truncated ? " · tail only" : ""}{s.incomplete_tail ? " · incomplete tail" : ""}</span></div>)}
    </details>
  </TerminalPanel>;
}

export function MLLab() {
  const query = useQuery({ queryKey: ["ml-lab"], queryFn: () => apiGet<MLLabPayload>("/api/ml-lab"), refetchInterval: 30_000, retry: false });
  return <MLLabView data={query.data} failed={query.isError} />;
}
