import { useMemo, useState } from "react";
import type { AgenticResearchStatus, BacktestRunSummary, ResearchPipelineCandidate, ResearchPipelinePayload, StrategyWorkflowRevision } from "../api";
import { useAgenticResearchStatus, useBacktestLab, useResearchPipeline, useResearchScorecard, useStrategyWorkflow } from "../queries";
import { BacktestLabPanel, StrategyWorkflowPanel } from "../panels/Panels";
import { DenseTable, TerminalBadge, TerminalPanel, type Column } from "./Terminal";
import { PatternAtlas } from "./PatternAtlas";

type ArenaView = "book" | "pipeline" | "backtests" | "forward" | "campaigns" | "agents" | "failures";

const ARENA_VIEWS: Array<{ id: ArenaView; label: string; eyebrow: string }> = [
  { id: "book", label: "The Book", eyebrow: "immutable revisions" },
  { id: "pipeline", label: "Pipeline", eyebrow: "idea to eligibility" },
  { id: "backtests", label: "Backtests", eyebrow: "canonical command centre" },
  { id: "forward", label: "Forward Queue", eyebrow: "trade-count evidence" },
  { id: "campaigns", label: "Campaigns", eyebrow: "bounded experiments" },
  { id: "agents", label: "Agent Scoreboard", eyebrow: "durability over luck" },
  { id: "failures", label: "Failure Archive", eyebrow: "retained negative evidence" },
];

export const PIPELINE_STAGES = [
  { id: "idea", label: "Idea", stages: ["PREREGISTERED"] },
  { id: "built", label: "Built", stages: ["REGISTERED"] },
  { id: "backtested", label: "Backtested", stages: ["BACKTESTED"] },
  { id: "oos", label: "OOS judged", stages: ["OOS_PASS", "OOS_REJECT"] },
  { id: "shadow", label: "Shadow", stages: ["SHADOW_OBSERVE"] },
  { id: "paper", label: "Paper", stages: [] },
  { id: "eligible", label: "Eligible", stages: [] },
] as const;

export function arenaPipelineCounts(revisions: StrategyWorkflowRevision[]) {
  return Object.fromEntries(PIPELINE_STAGES.map((step) => {
    let count = revisions.filter((row) => (step.stages as readonly string[]).includes(row.stage)).length;
    if (step.id === "paper") {
      count = revisions.filter((row) => row.latest_run?.data_provenance === "paper_trial" || row.latest_judgment?.run_kind === "paper_trial").length;
    }
    if (step.id === "eligible") {
      count = revisions.filter((row) => row.can_trade || row.can_promote).length;
    }
    return [step.id, count];
  })) as Record<(typeof PIPELINE_STAGES)[number]["id"], number>;
}

const declaredCount = (summary: Record<string, unknown>, keys: string[]) => keys.reduce((total, key) => {
  const value = summary[key];
  return total + (typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : 0);
}, 0);

export interface ArenaAgentRank {
  agentId: string;
  role: string;
  health: number;
  durable: number;
  waste: number;
  actions: number;
  status: string;
}

export function arenaAgentRanking(scorecards: NonNullable<AgenticResearchStatus["agent_scorecards"]>): ArenaAgentRank[] {
  return scorecards.map((row) => ({
    agentId: row.agent_id,
    role: row.role,
    health: row.health_score,
    durable: declaredCount(row.summary, ["sample_valid", "ready_for_untouched_judgment", "promotable_proofs", "passes_on_untouched", "oos_pass"]),
    waste: declaredCount(row.summary, ["rejected", "failed", "retired", "decayed", "negative", "invalid"]),
    actions: row.action_count,
    status: row.status,
  })).sort((a, b) => b.durable - a.durable || a.waste - b.waste || b.health - a.health || a.agentId.localeCompare(b.agentId));
}

function formatMoney(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value < 0 ? "−" : value > 0 ? "+" : ""}$${Math.abs(value).toFixed(2)}`;
}

function titleCase(value: string | null | undefined) {
  return String(value || "not reported").replace(/_/g, " ").toLowerCase();
}

function primaryCostProfile(row: StrategyWorkflowRevision) {
  const routes = Object.values(row.params?.execution_policy ?? {});
  const profiles = Array.from(new Set(routes.map((route) => route.cost_profile_id).filter(Boolean)));
  return profiles.join(" / ") || "not reported";
}

function entryClock(row: StrategyWorkflowRevision) {
  return row.params?.runtime?.entry_clock || "not reported";
}

export function experimentReadout(row: ResearchPipelineCandidate) {
  return {
    preflight: row.preflight?.status ?? "UNVERIFIED",
    audit: row.falsification?.status ?? "UNVERIFIED",
    causal: row.causality?.passed == null ? "not tested" : row.causality.passed ? "yes" : "no",
    gaps: Array.from(new Set([...(row.reasons ?? []), ...(row.preflight?.failures ?? []), ...(row.falsification?.unverified ?? [])])),
  };
}

export function ContinuousPipeline({ pipeline }: { pipeline: ResearchPipelinePayload | undefined }) {
  const candidates = pipeline?.candidates ?? [];
  const verdictTone = (verdict: string): "good" | "bad" | "warn" => verdict === "CANDIDATE" ? "good" : verdict.startsWith("REFUSED") || verdict === "ERROR" ? "bad" : "warn";
  return (
    <div className="arena-surface p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="eyebrow">Continuous AI / ML research conveyor</div>
          <h3 className="mt-2 text-[17px] font-semibold">Propose → preflight → freeze → test → challenge</h3>
          <p className="mt-2 max-w-3xl text-[11px] leading-6 text-dim">One bounded proposal per cycle. Data, clock and costs must be declared before replay. Independent audit is deterministic, not an AI vote. Rolling OOS remains exploratory; missing funding, cost stress and execution parity stay visible. No result can promote or trade.</p>
        </div>
        <div className="flex gap-2">
          <TerminalBadge tone={pipeline?.artifact_available ? "good" : "warn"}>{titleCase(pipeline?.status ?? "not started")}</TerminalBadge>
          <TerminalBadge tone="bad">authority false</TerminalBadge>
        </div>
      </div>
      <p className="mt-4 text-[11px] text-dim" aria-label="Research queue status">
        Discovered {pipeline?.summary.discovered_candidates ?? "—"} · attempted {pipeline?.summary.attempted_candidates ?? "—"} · deferred {pipeline?.summary.deferred_candidates ?? "—"} · backtested {pipeline?.summary.backtested_candidates ?? "—"}.
        {" "}Next queue: {pipeline?.next_queue_at ?? "none scheduled"} · next retest: {pipeline?.next_retest_at ?? "unreported"}.
        {" "}Scheduled eligibility times; execution waits for the worker cycle.
      </p>
      <div className="arena-auto-stages mt-5">
        {(pipeline?.stages ?? []).map((stage) => <div key={stage.key}><span>{stage.label}</span><strong>{stage.count}</strong><small>{titleCase(stage.state)}</small></div>)}
        {!pipeline?.stages?.length && <div className="arena-auto-stage-empty">Start the optional research profile to publish the first cycle.</div>}
      </div>
      {pipeline?.lake_repair && <div className="mt-4 text-[11px] text-dim">
        Last lake repair audit: {pipeline.lake_repair.generated_at ?? "unreported"}.
        {pipeline.lake_repair.reason && <p className="text-warn">{pipeline.lake_repair.reason}</p>}
        {Object.entries(pipeline.lake_repair.symbols ?? {}).map(([symbol, audit]) => <p key={symbol}>
          {symbol}: verified 1h {audit.levels?.["1h"]?.verified_bars ?? "—"} / {audit.arena_required_1h ?? "—"};
          {" "}missing verified slots {audit.levels?.["1h"]?.missing_internal_slots ?? "—"};
          {" "}raw days {audit.raw_days ?? "—"}. {titleCase(audit.historical_coverage ?? "coverage unreported")}.
          {" "}Unit-corrected partial rows are not eligible history.
        </p>)}
      </div>}
      <div className="mt-4 grid gap-3 lg:grid-cols-[1fr_280px]">
        <div className="overflow-x-auto">
          <table className="arena-candidate-table">
            <thead><tr><th>Candidate / frozen experiment</th><th>Verdict / audit</th><th>Causal</th><th>OOS trades</th><th>Booked net*</th></tr></thead>
            <tbody>{candidates.map((row) => {
              const proof = experimentReadout(row);
              return <tr key={row.evidence_id ?? row.strategy_id}>
                <td><b>{row.strategy_id}</b><small>packet {row.packet_id?.slice(0, 12) ?? "missing"} · source {row.source_sha256?.slice(0, 8) ?? "unverified"}</small>
                  <small>{row.entry_clock ?? "clock unverified"} · {row.cost_profile_id ?? "cost unverified"} · {row.booked_round_bps == null ? "—" : `${row.booked_round_bps.toFixed(2)} bps / round`}</small>
                  <details className="mt-2 max-w-lg"><summary className="cursor-pointer text-accent">Inspect evidence and gaps</summary>
                    <p className="mt-2">Preflight: {titleCase(proof.preflight)}. Bars: {row.preflight?.bars_available ?? "—"} / {row.preflight?.bars_required ?? "—"}; warmup {row.preflight?.warmup_bars ?? "—"}.</p>
                    <p className="mt-2 break-all">Packet: {row.packet_id ?? "missing"}<br />Dataset: {row.dataset_sha256 ?? "missing"}<br />Attempt: {row.attempt_id ?? "missing"}</p>
                    <p className="mt-2">Verified checks: {row.falsification?.agreed.map(titleCase).join(" · ") || "none reported"}</p>
                    <p className="mt-2 text-short">Challenges: {row.falsification?.contested.map(titleCase).join(" · ") || "none reported—not approval"}</p>
                    <p className="mt-2 text-warn">Unresolved: {proof.gaps.map(titleCase).join(" · ") || "none reported—not promotion"}</p>
                  </details>
                </td>
                <td><TerminalBadge tone={verdictTone(row.verdict)}>{titleCase(row.verdict)}</TerminalBadge><small>{titleCase(proof.audit)}</small></td>
                <td>{proof.causal}</td><td>{row.walk_forward?.oos_trades ?? "—"}</td>
                <td className={(row.walk_forward?.oos_net_usd ?? 0) < 0 ? "text-short" : "text-long"}>{formatMoney(row.walk_forward?.oos_net_usd)}</td>
              </tr>;
            })}</tbody>
          </table>
          {!candidates.length && <div className="arena-empty"><strong>No candidate evidence yet.</strong><span>The display stays empty until a real sandbox/OOS cycle publishes.</span></div>}
          <p className="mt-3 text-[10px] text-dim">*Research backtest only. Funding excluded on governed packets; product limits are unverified research assumptions. This is not operational execution PnL.</p>
        </div>
        <div className="arena-ml-card">
          <div className="eyebrow">ML meta-label gate</div>
          <strong>{pipeline?.ml.samples ?? 0}<small> / {pipeline?.ml.min_to_train ?? 200}</small></strong>
          <div className="arena-progress"><i style={{ width: `${Math.min(100, (pipeline?.ml.samples ?? 0) / Math.max(1, pipeline?.ml.min_to_train ?? 200) * 100)}%` }} /></div>
          <p>{titleCase(pipeline?.ml.stage ?? "unavailable")}</p>
          <span>binding=false · can_trade=false</span>
        </div>
      </div>
    </div>
  );
}

function PipelineView({ revisions, pipeline }: { revisions: StrategyWorkflowRevision[]; pipeline: ResearchPipelinePayload | undefined }) {
  const counts = arenaPipelineCounts(revisions);
  const oosPass = revisions.filter((row) => row.stage === "OOS_PASS").length;
  const oosReject = revisions.filter((row) => row.stage === "OOS_REJECT").length;
  return (
    <div className="space-y-4">
      <ContinuousPipeline pipeline={pipeline} />
      <div className="arena-pipeline" aria-label="Research strategy pipeline">
        {PIPELINE_STAGES.map((step, index) => (
          <div key={step.id} className={`arena-pipeline__step ${step.id === "eligible" ? "arena-pipeline__step--locked" : ""}`}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <strong>{counts[step.id]}</strong>
            <b>{step.label}</b>
            {index < PIPELINE_STAGES.length - 1 && <i aria-hidden="true">→</i>}
          </div>
        ))}
      </div>
      <div className="grid gap-3 lg:grid-cols-[1.25fr_.75fr]">
        <div className="arena-surface p-5">
          <div className="eyebrow">Promotion physics</div>
          <h3 className="mt-2 text-[17px] font-semibold">A result advances only when the evidence class advances.</h3>
          <p className="mt-2 max-w-3xl text-[11px] leading-6 text-dim">A backtest does not become shadow evidence, and shadow elapsed days do not substitute for resolved trades. Engine parity, untouched OOS, execution identity and human review remain separate gates.</p>
          <div className="mt-5 grid grid-cols-2 gap-2 md:grid-cols-4">
            <ArenaMetric label="OOS pass" value={oosPass} tone="good" />
            <ArenaMetric label="OOS reject" value={oosReject} tone="bad" />
            <ArenaMetric label="Shadow revisions" value={counts.shadow} tone="info" />
            <ArenaMetric label="Capital eligible" value={counts.eligible} tone="warn" />
          </div>
        </div>
        <div className="arena-surface p-5">
          <div className="eyebrow">Authority wall</div>
          <div className="mt-4 flex items-center justify-between border-b border-line/60 pb-3"><span className="text-[11px] text-dim">Arena can trade</span><TerminalBadge tone="bad">false</TerminalBadge></div>
          <div className="flex items-center justify-between border-b border-line/60 py-3"><span className="text-[11px] text-dim">Arena can promote</span><TerminalBadge tone="bad">false</TerminalBadge></div>
          <div className="flex items-center justify-between pt-3"><span className="text-[11px] text-dim">Human review</span><TerminalBadge tone="warn">required</TerminalBadge></div>
        </div>
      </div>
    </div>
  );
}

function ArenaMetric({ label, value, tone = "neutral" }: { label: string; value: number; tone?: "neutral" | "good" | "warn" | "bad" | "info" }) {
  const colors = { neutral: "text-txt", good: "text-long", warn: "text-warn", bad: "text-short", info: "text-info" };
  return <div className="arena-metric"><span>{label}</span><strong className={colors[tone]}>{value}</strong></div>;
}

function ForwardQueue({ revisions, minimumSamples }: { revisions: StrategyWorkflowRevision[]; minimumSamples: number }) {
  const rows = revisions.filter((row) => row.stage === "SHADOW_OBSERVE" || row.shadow_evidence != null).map((row) => {
    const resolved = Number(row.shadow_evidence?.virtual_resolved ?? 0);
    const pending = Number(row.shadow_evidence?.virtual_pending ?? 0);
    const accepted = Number(row.shadow_evidence?.accepted_entries ?? row.shadow_evidence?.virtual_approved ?? 0);
    return { row, resolved, pending, accepted, target: minimumSamples, progress: Math.min(100, resolved / Math.max(1, minimumSamples) * 100) };
  });
  return (
    <TerminalPanel title="Forward Test Queue" meta="progress is resolved trades · never elapsed days">
      {!rows.length ? <div className="arena-empty"><strong>No forward evidence is currently bound.</strong><span>A strategy appears here only after its immutable revision has shadow or paper evidence.</span></div> : (
        <div className="grid gap-3 xl:grid-cols-2">
          {rows.map(({ row, resolved, pending, accepted, target, progress }) => (
            <article key={row.revision_id} className="arena-forward-card">
              <div className="flex items-start justify-between gap-3"><div><div className="eyebrow">{row.symbols.join(" · ") || "unbound market"}</div><h3>{row.strategy_id}</h3></div><TerminalBadge tone={row.shadow_evidence?.performance_eligible ? "good" : "warn"}>{row.shadow_evidence?.performance_eligible ? "eligible evidence" : "diagnostic only"}</TerminalBadge></div>
              <div className="mt-5 flex items-end justify-between"><strong className="font-mono text-3xl">{resolved}<small> / {target}</small></strong><span className="font-mono text-[10px] text-faint">{accepted} accepted · {pending} pending</span></div>
              <div className="arena-progress mt-3"><i style={{ width: `${progress}%` }} /></div>
              <div className="mt-4 grid grid-cols-2 gap-2 text-[10px] text-dim"><span>clock <b>{titleCase(entryClock(row))}</b></span><span>cost <b>{primaryCostProfile(row)}</b></span></div>
            </article>
          ))}
        </div>
      )}
    </TerminalPanel>
  );
}

function Campaigns({ runs, actions, onNavigate }: { runs: BacktestRunSummary[]; actions: NonNullable<AgenticResearchStatus["operator_queue"]>; onNavigate: (tab: string) => void }) {
  const columns: Column<BacktestRunSummary>[] = [
    { key: "run", header: "Campaign / run", render: (row) => <span className="block min-w-[220px]"><b className="font-mono text-txt">{row.strategy_id ?? "unknown strategy"}</b><small className="block max-w-[280px] truncate text-[9px] text-faint" title={row.run_id}>{row.run_id}</small></span> },
    { key: "cell", header: "Market / clock", render: (row) => <span className="font-mono text-[10px]">{row.exchange ?? "—"} · {row.symbol ?? "—"} · {row.timeframe ?? "—"}</span> },
    { key: "status", header: "State", render: (row) => <TerminalBadge tone={row.status === "COMPLETE" ? "good" : row.status === "FAILED" ? "bad" : "info"}>{titleCase(row.status)}</TerminalBadge> },
    { key: "sample", header: "Trades", align: "right", render: (row) => row.num_trades ?? "—" },
    { key: "net", header: "Booked net", align: "right", render: (row) => <span className={(row.net_profit_usd ?? 0) < 0 ? "text-short" : "text-long"}>{formatMoney(row.net_profit_usd)}</span> },
    { key: "proof", header: "Fingerprint", render: (row) => <span className="block min-w-[150px]"><b className="font-mono text-[10px]">{row.bundle_id ? row.bundle_id.slice(0, 12) : "no bundle"}</b><small className="block text-[9px] text-faint">{row.parity_status ?? "parity not reported"} · {row.code_sha?.slice(0, 8) ?? "code sha missing"}</small></span> },
  ];
  return (
    <div className="space-y-4">
      <TerminalPanel title="Optimisation Campaigns" meta="bounded runs · one evidence fingerprint per result">
        <DenseTable columns={columns} rows={runs.slice(0, 40)} rowKey={(row) => row.run_id} empty="No bounded campaign runs are recorded." />
      </TerminalPanel>
      <TerminalPanel title="Research Work Queue" meta="agent proposals · no automatic execution">
        {!actions.length ? <div className="arena-empty"><strong>No agent work is queued.</strong><span>The absence of synthetic activity is intentional.</span></div> : <div className="grid gap-2 md:grid-cols-2">{actions.slice(0, 12).map((action, index) => <div key={`${action.entity_id ?? action.action}:${index}`} className="arena-task"><TerminalBadge tone={action.severity === "critical" ? "bad" : action.severity === "warning" ? "warn" : "neutral"}>{action.severity ?? "info"}</TerminalBadge><div><strong>{titleCase(action.action)}</strong><span>{action.reason || "Reason not reported"}</span></div></div>)}</div>}
      </TerminalPanel>
      <PatternAtlas onNavigate={onNavigate} />
    </div>
  );
}

function AgentScoreboard({ scorecards }: { scorecards: NonNullable<AgenticResearchStatus["agent_scorecards"]> }) {
  const ranking = arenaAgentRanking(scorecards);
  const columns: Column<ArenaAgentRank>[] = [
    { key: "rank", header: "Rank / research agent", render: (row) => <span className="block min-w-[220px]"><b className="font-mono text-txt">#{ranking.indexOf(row) + 1} · {row.agentId}</b><small className="block text-[9px] text-faint">{row.role}</small></span> },
    { key: "durable", header: "Durable proofs", align: "right", render: (row) => <span className="text-long">{row.durable}</span> },
    { key: "waste", header: "Declared waste", align: "right", render: (row) => <span className={row.waste ? "text-short" : "text-dim"}>{row.waste}</span> },
    { key: "health", header: "Evidence health", align: "right", render: (row) => `${row.health.toFixed(0)}%` },
    { key: "actions", header: "Open actions", align: "right", render: (row) => row.actions },
    { key: "status", header: "Status", render: (row) => <TerminalBadge tone={row.status === "HEALTHY" ? "good" : "warn"}>{titleCase(row.status)}</TerminalBadge> },
  ];
  return <TerminalPanel title="Agent Arena" meta="rank: durable declared proofs ↓ · wasted declared runs ↑ · evidence health ↓"><DenseTable columns={columns} rows={ranking} rowKey={(row) => row.agentId} empty="Agent scorecards are unavailable. No ranking is asserted." /><div className="mt-3 text-[9px] leading-5 text-faint">Ranking uses only counters explicitly published by each research agent. It does not reward raw PnL, sparse winners, or the number of ideas generated.</div></TerminalPanel>;
}

function FailureArchive({ revisions }: { revisions: StrategyWorkflowRevision[] }) {
  const failures = revisions.filter((row) => ["QUARANTINED", "KILLED", "OOS_REJECT"].includes(row.stage));
  return <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{failures.map((row) => <article key={row.revision_id} className="arena-failure-card"><div className="flex items-start justify-between gap-3"><div><div className="eyebrow">{row.parent_revision_id ? "failed fork" : "root evidence"}</div><h3>{row.strategy_id}</h3></div><TerminalBadge tone="bad">{titleCase(row.stage)}</TerminalBadge></div><p>{row.status_reason || row.governance_flags.join(" · ") || "No rejection reason was recorded."}</p><dl><div><dt>Booked net</dt><dd>{formatMoney(row.performance.after_cost_net_usd)}</dd></div><div><dt>Trades</dt><dd>{row.performance.trades ?? "—"}</dd></div><div><dt>PF</dt><dd>{row.performance.profit_factor?.toFixed(2) ?? "—"}</dd></div><div><dt>Max DD</dt><dd>{row.performance.max_drawdown_pct == null ? "—" : `${row.performance.max_drawdown_pct.toFixed(2)}%`}</dd></div></dl><footer><span>{titleCase(entryClock(row))}</span><span>{primaryCostProfile(row)}</span></footer></article>)}</div>;
}

export function ResearchArena({ onNavigate }: { onNavigate: (tab: string) => void }) {
  const [view, setView] = useState<ArenaView>("pipeline");
  const workflow = useStrategyWorkflow();
  const lab = useBacktestLab();
  const scorecard = useResearchScorecard();
  const agents = useAgenticResearchStatus();
  const pipeline = useResearchPipeline();
  const revisions = workflow.data?.revisions ?? [];
  const failures = revisions.filter((row) => ["QUARANTINED", "KILLED", "OOS_REJECT"].includes(row.stage)).length;
  const activeProofs = (lab.data?.runs ?? []).filter((row) => !["COMPLETE", "FAILED", "REJECTED"].includes(row.status)).length;
  const selected = useMemo(() => ARENA_VIEWS.find((item) => item.id === view) ?? ARENA_VIEWS[1], [view]);
  return (
    <main className="research-arena space-y-3">
      <section className="arena-hero">
        <div><div className="eyebrow">VNEDGE / research factory</div><h1>Harvest mechanisms.<br /><span>Sweep markets. Barely tune.</span></h1><p>Every revision survives as evidence. Winners earn harder tests; failures remain searchable. Nothing here can trade or promote itself.</p></div>
        <div className="arena-hero__metrics"><ArenaMetric label="Book revisions" value={revisions.length} tone="info" /><ArenaMetric label="Active proofs" value={activeProofs} tone="warn" /><ArenaMetric label="OOS passes" value={workflow.data?.summary.oos_pass ?? 0} tone="good" /><ArenaMetric label="Failures retained" value={failures} tone="bad" /></div>
        <div className="arena-hero__lock"><span>AUTHORITY</span><strong>RESEARCH ONLY</strong><small>can_trade=false · can_promote=false</small></div>
      </section>
      <nav className="arena-nav" aria-label="Research Arena views">{ARENA_VIEWS.map((item) => <button key={item.id} type="button" onClick={() => setView(item.id)} className={view === item.id ? "is-active" : ""}><span>{item.label}</span><small>{item.eyebrow}</small></button>)}</nav>
      {(workflow.isError || lab.isError || agents.isError || pipeline.isError) && <div className="rounded-lg border border-warn/40 bg-warn/5 px-4 py-3 text-[11px] text-warn" role="status">Some Arena evidence sources are unavailable. Missing records remain unknown; zero is not substituted.</div>}
      <div className="arena-view-head"><div><span>{selected.eyebrow}</span><h2>{selected.label}</h2></div><p>Read-only evidence projection · immutable IDs · after-cost metrics</p></div>
      {view === "book" && <StrategyWorkflowPanel />}
      {view === "pipeline" && <PipelineView revisions={revisions} pipeline={pipeline.data} />}
      {view === "backtests" && <BacktestLabPanel />}
      {view === "forward" && <ForwardQueue revisions={revisions} minimumSamples={scorecard.data?.performance_policy.min_samples ?? 30} />}
      {view === "campaigns" && <Campaigns runs={lab.data?.runs ?? []} actions={agents.data?.operator_queue ?? []} onNavigate={onNavigate} />}
      {view === "agents" && <AgentScoreboard scorecards={agents.data?.agent_scorecards ?? []} />}
      {view === "failures" && <FailureArchive revisions={revisions} />}
    </main>
  );
}
