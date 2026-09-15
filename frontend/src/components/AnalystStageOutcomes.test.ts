import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StageOutcomesFacts, type StageOutcomesReport } from "./AnalystStageOutcomes";

const report = (): StageOutcomesReport => ({ state: "current", generated_at: new Date().toISOString(), contexts: [], groups: [], audits: [] });
describe("Stage outcome evidence", () => {
  it("never turns missing labels into zero-return trades", () => {
    const html = renderToStaticMarkup(createElement(StageOutcomesFacts, {data: report()}));
    expect(html).toContain("Awaiting verified paper outcomes");
    expect(html).toContain("not zero-return trades");
    expect(html).toContain("No signal filters");
    expect(html).not.toContain("net $0");
  });
  it("hides stale results and cached results after a connection failure", () => {
    for (const props of [{data: {...report(), state: "stale"}}, {data: report(), failed: true}, {data: {...report(),generated_at: "2020-01-01T00:00:00Z"}}]) {
      const html = renderToStaticMarkup(createElement(StageOutcomesFacts, props));
      expect(html).toContain("No current outcome conclusion");
      expect(html).not.toContain("captured records");
    }
  });
  it("distinguishes an unstarted worker from a healthy empty report", () => {
    const html = renderToStaticMarkup(createElement(StageOutcomesFacts, {data: {...report(),state:"worker_not_started"}}));
    expect(html).toContain("Stage recorder has not started");
  });
  it("labels partial coverage and diagnostic captures without counting trades", () => {
    const html = renderToStaticMarkup(createElement(StageOutcomesFacts, {data: {...report(),contexts_truncated:true,
      contexts:[{row_key:"r",strategy_id:"s",cutoff:"cutoff",decision_id:null,stages:[{timeframe:"4h",stage:"advancing",state:"unavailable",issues:["source_missing"]}]}]}}));
    expect(html).toContain("Coverage is incomplete");
    expect(html).toContain("diagnostic evaluation, not a trade");
    expect(html).toContain("context unavailable at decision");
    expect(html).not.toContain("4h: advancing");
  });
});
