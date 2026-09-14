import { beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { BacktestRunSummary, CorrectionLane, JournalPayload, StrategyWorkflowRevision } from "../api";

const query = vi.hoisted(() => ({ data: undefined as unknown, isError: false, isLoading: false }));
vi.mock("../queries", () => ({
  useSnapshot: () => query, useLanes: () => query, useJournal: () => query,
  useMeta: () => query, useRiskSnapshot: () => query,
}));
import { BookPanel, JournalPanel, shadowNetTotal } from "../panels/Panels";
import { CockpitCommandBar } from "./CockpitCommandBar";
import { activeProofCount, ForwardQueue } from "./ResearchArena";

describe("dashboard missing-evidence regression", () => {
  beforeEach(() => { query.data = undefined; query.isError = false; });
  it("never sums missing, stale or partially reported shadow lanes as zero", () => {
    const lane = { observation_class: "shadow_observe", shadow_perf: { virtual_net_usd: 0 } } as CorrectionLane;
    expect(shadowNetTotal(undefined, true)).toBeNull();
    expect(shadowNetTotal([], true)).toBeNull();
    expect(shadowNetTotal([lane], false)).toBeNull();
    expect(shadowNetTotal([lane, { observation_class: "shadow_observe" } as CorrectionLane], true)).toBeNull();
    expect(shadowNetTotal([lane], true)).toBe(0);
    expect(renderToStaticMarkup(createElement(BookPanel))).not.toContain("$0.00");
  });
  it("does not declare kill/halt clear or readiness passed without telemetry", () => {
    const html = renderToStaticMarkup(createElement(CockpitCommandBar, { onOpenRisk: () => {} }));
    expect(html).toContain("operational snapshot unavailable or stale");
    expect(html).toContain("kill unknown");
    expect(html).not.toContain("halt clear");
    expect(html).not.toContain("PASS");
  });
  it("does not reconcile absent history or count unknown open orders as zero", () => {
    const html = renderToStaticMarkup(createElement(JournalPanel));
    expect(html).toContain("missing source proof");
    expect(html).not.toContain("matched");
    expect(html).not.toContain("$0.00");
  });
  it("suppresses diagnostic sums when the backend reports partial source coverage", () => {
    query.data = { source_coverage: { state: "partial" }, summary: { headline_actual_closed_net_usd: 0, virtual_net_usd: 0 }, closed_trades: [], events: [], scanner_events: [] };
    expect(renderToStaticMarkup(createElement(JournalPanel))).not.toContain("$0.00");
  });
  it("describes valid empty read-window arithmetic without claiming ledger reconciliation", () => {
    query.data = { source_coverage: { state: "bounded_window" }, summary: { actual_closed_net_usd: 0, headline_actual_closed_net_usd: 0 }, closed_trades: [], events: [], scanner_events: [] } as unknown as JournalPayload;
    const html = renderToStaticMarkup(createElement(JournalPanel));
    expect(html).toContain("window totals agree (not ledger reconciliation)");
    expect(html).toContain("$0.00");
  });
  it("counts only explicitly active jobs, not terminal or unknown statuses", () => {
    const runs = ["QUEUED", "RUNNING", "PENDING", "DONE_RESEARCH_ONLY", "COMPLETE", "FAILED", "REJECTED", "UNKNOWN"].map(status => ({ status } as BacktestRunSummary));
    expect(activeProofCount(runs)).toBe(3);
  });
  it("does not fabricate zero forward outcomes for registered observers", () => {
    const revision = { revision_id: "research", strategy_id: "research", stage: "SHADOW_OBSERVE", symbols: [], params: {} } as unknown as StrategyWorkflowRevision;
    const html = renderToStaticMarkup(createElement(ForwardQueue, { revisions: [revision], minimumSamples: 30 }));
    expect(html).toContain("Forward outcome evidence not reported");
    expect(html).not.toContain("0 accepted");
    expect(html).not.toContain("width:0%");
  });
});
