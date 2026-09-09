import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { AgenticResearchStatus, ResearchPipelineCandidate, ResearchPipelinePayload, StrategyWorkflowRevision } from "../api";
import { arenaAgentRanking, arenaPipelineCounts, ContinuousPipeline, experimentReadout } from "./ResearchArena";

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
