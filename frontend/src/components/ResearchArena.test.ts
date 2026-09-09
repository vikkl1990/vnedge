import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { AgenticResearchStatus, ResearchDataReadiness, ResearchPipelineCandidate, ResearchPipelinePayload, StrategyWorkflowRevision } from "../api";
import { arenaAgentRanking, arenaPipelineCounts, ContinuousPipeline, ExperimentDataReadiness, experimentReadout, RecoveryPlan } from "./ResearchArena";

const revision = (stage: string, extra: Partial<StrategyWorkflowRevision> = {}) => ({
  revision_id: `${stage}:revision`,
  strategy_id: `${stage.toLowerCase()}_v1`,
  version: "1",
  parent_revision_id: null,
  stage,
  status: "REGISTERED",
  status_reason: "",
  timeframes: ["15m"],
  symbols: ["BTCUSD"],
  backtest_engine: "vnedge",
  engine_version: "1",
  parity_status: "NOT_REPORTED" as const,
  preregistration: "",
  governance_flags: [],
  performance: { after_cost_net_usd: null, trades: null, profit_factor: null, max_drawdown_pct: null, sample_qualified: false },
  latest_run: null,
  can_trade: false as const,
  can_promote: false as const,
  ...extra,
});

describe("research arena projections", () => {
  it("renders recovery evidence without granting write authority", () => {
    const html = renderToStaticMarkup(createElement(RecoveryPlan, { report: {
      generated_at: "2026-09-09T03:00:00Z", symbols: { BTCUSD: {
        status: "GAPS_REMAIN", plan_id: "proof-id", required_hours: 2160,
        counts: { VERIFIED: 189, RAW_DAY_PRESENT_COVERAGE_UNPROVEN: 20 },
        ranges: [{ open_time: "2026-09-01T00:00:00Z", close_time: "2026-09-01T01:00:00Z", hours: 1, status: "PRESENT_PROOF_INVALID" }],
      } },
    } }));
    expect(html).toContain("189 / 2160 hours");
    expect(html).toContain("proof-id");
    expect(html).toContain("raw day present coverage unproven: 20 hours");
    expect(html).toContain("not experiment admission");
    expect(html).toContain("end exclusive");
    expect(html).not.toContain("<button");
  });
  it("does not present missing recovery reports as success", () => {
    const html = renderToStaticMarkup(createElement(RecoveryPlan, { report: undefined }));
    expect(html).toContain("missing evidence is not zero gaps");
    expect(html).not.toContain("0 / 2160");
  });
  it("shows historical coverage diagnostics without implying approval or repair", () => {
    const data: ResearchDataReadiness = {
      schema_version: 1, scope: "supplied_experiment_frame", observed_at: "2026-09-09T03:00:00Z",
      exchange: "delta_india", symbol: "BTC/USD:USD", timeframe: "1h",
      stored_rows: 196, verified_unique_bars: 189, required_bars: 2160,
      row_shortfall: 1964, contiguous_shortfall: 2060,
      first_open: "2026-09-01T00:00:00Z", last_open: "2026-09-09T01:00:00Z",
      longest_contiguous_bars: 100, latest_contiguous_bars: 10,
      longest_from_open: "2026-09-01T00:00:00Z", longest_to_open: "2026-09-05T03:00:00Z",
      missing_internal_bars: 5, gap_range_count: 1,
      gap_ranges: [{ from_open: "2026-09-06T00:00:00Z", to_open: "2026-09-06T04:00:00Z", missing_bars: 5 }],
      gap_ranges_truncated: false, duplicate_slots: 0, out_of_order_rows: 0,
      invalid_row_counts: { coverage_unproven: 7 }, source_counts: { canonical_tick_lake: 196 },
      blockers: ["history_shortfall", "row_proof_failures", "internal_gaps"],
      historical_coverage: "outside_frame_unknown", repair_authorized: false, can_trade: false, can_promote: false,
    };
    const html = renderToStaticMarkup(createElement(ExperimentDataReadiness, { data }));
    expect(html).toContain("196 / 2160");
    expect(html).toContain("Verified unique bars");
    expect(html).toContain("coverage unproven × 7");
    expect(html).toContain("5 missing bars");
    expect(html).toContain("not current live readiness");
    expect(html).toContain("does not authorize trimming the frozen window");
    expect(html).toContain("no bars are filled here");
  });
  it("keeps legacy attempts explicitly unmeasured", () => {
    const html = renderToStaticMarkup(createElement(ExperimentDataReadiness, {}));
    expect(html).toContain("Data diagnostics unavailable for this attempt");
    expect(html).not.toContain("0 / 2160");
  });
  it("separates inventory from attempts and renders every deferred candidate", () => {
    const pipeline: ResearchPipelinePayload = {
      pipeline_id: "continuous_ai_research_v2", evaluation_status: "CACHED",
      policy: { can_trade: false, can_promote: false },
      can_trade: false, can_promote: false, live_orders_enabled: false,
      status: "BLOCKED_EVIDENCE", stages: [],
      summary: { discovered_candidates: 16, attempted_candidates: 8, deferred_candidates: 8, backtested_candidates: 0 },
      next_queue_at: "2026-09-09T02:00:00+00:00", next_retest_at: "2026-09-10T01:00:00+00:00",
      candidates: Array.from({ length: 16 }, (_, i) => ({ strategy_id: `candidate_${i}`, source_file: `${i}.py`, verdict: "DEFERRED_BUDGET", reasons: [], can_trade: false, can_promote: false })),
      ml: { samples: 0, min_to_train: 200, stage: "COLLECTING_LABELS", binding: false, can_trade: false },
    };
    const html = renderToStaticMarkup(createElement(ContinuousPipeline, { pipeline }));
    expect(html).toContain("Discovered 16 · attempted 8 · deferred 8 · backtested 0");
    expect(html).toContain("candidate_15");
    expect(html).toContain("2026-09-09T02:00:00+00:00");
    expect(html).toContain("execution waits for the worker cycle");
  });
  it("does not turn missing preflight or causality into a result", () => {
    const row: ResearchPipelineCandidate = { strategy_id: "ai_test", source_file: "test.py",
      verdict: "NOT_TESTABLE", reasons: ["history_insufficient"], can_trade: false, can_promote: false };
    expect(experimentReadout(row)).toEqual({ preflight: "UNVERIFIED", audit: "UNVERIFIED",
      causal: "not tested", gaps: ["history_insufficient"] });
  });

  it("renders the research boundary without inventing a running swarm", () => {
    const html = renderToStaticMarkup(createElement(ContinuousPipeline, { pipeline: undefined }));
    expect(html).toContain("Independent audit is deterministic, not an AI vote");
    expect(html).toContain("No candidate evidence yet");
    expect(html).toContain("authority false");
  });

  it("keeps OOS failures in the pipeline and never infers capital eligibility", () => {
    const counts = arenaPipelineCounts([
      revision("REGISTERED"),
      revision("OOS_PASS"),
      revision("OOS_REJECT"),
      revision("SHADOW_OBSERVE", { shadow_evidence: { virtual_resolved: 7 } }),
    ]);
    expect(counts).toMatchObject({ built: 1, oos: 2, shadow: 1, eligible: 0 });
  });

  it("ranks declared durable proof ahead of health and idea volume", () => {
    const cards = [
      {
        agent_id: "idea_machine",
        source: "ideas",
        role: "generates ideas",
        health_score: 100,
        freshness_minutes: 1,
        critical_actions: 0,
        warning_actions: 0,
        action_count: 0,
        summary: { rejected: 8 },
        status: "HEALTHY",
        can_trade: false as const,
        can_promote: false as const,
      },
      {
        agent_id: "verifier",
        source: "proof",
        role: "verifies candidates",
        health_score: 80,
        freshness_minutes: 5,
        critical_actions: 0,
        warning_actions: 0,
        action_count: 1,
        summary: { sample_valid: 2, failed: 1 },
        status: "HEALTHY",
        can_trade: false as const,
        can_promote: false as const,
      },
    ] satisfies NonNullable<AgenticResearchStatus["agent_scorecards"]>;
    const ranking = arenaAgentRanking(cards);
    expect(ranking.map((row) => row.agentId)).toEqual(["verifier", "idea_machine"]);
    expect(ranking[0]).toMatchObject({ durable: 2, waste: 1 });
  });
});
