import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AnalystContext, type MarketStage, type Fundamentals } from "./AnalystContext";

const stage: MarketStage = {version:"market_stage_analyst_v1", spec_hash:"frozen-spec", timeframe:"4h", state:"current", stage:"base_after_decline", previous_stage:"transition", stage_id:"bar-proof", as_of:new Date().toISOString(), since:new Date().toISOString(), bars_in_state:4, supports:["Flat EMA"], conflicts:["weak_participation"], issues:[], transitions:[{from:"transition",to:"base_after_decline",at:new Date().toISOString()}],watch:[{toward:"advancing_trend",condition:"Two closed confirmations above 200",level:200}]};
describe("Stage and fundamentals", () => {
  it("explains memory and confirmation without claiming institutional certainty", () => {
    const html = renderToStaticMarkup(createElement(AnalystContext,{stages:[stage]}));
    expect(html).toContain("base after decline"); expect(html).toContain("4 closed bars");
    expect(html).toContain("Two closed confirmations"); expect(html).toContain("Bounded history");
    expect(html).toContain("hypotheses"); expect(html).toContain("bar-proof");
  });
  it("never presents an expired stage as current", () => {
    const html = renderToStaticMarkup(createElement(AnalystContext,{stages:[{...stage,as_of:"2020-01-01T00:00:00Z"}]}));
    expect(html).toContain("stale"); expect(html).toContain("No current stage conclusion");
    expect(html).not.toContain("Two closed confirmations");
  });
  it("distinguishes missing, not applicable and real zero; rejects stale amounts", () => {
    const now=new Date().toISOString();
    const fundamentals: Fundamentals={version:"v1",template:"proof_of_work_network",health:"not_assessed",note:"Separate measures",fields:[
      {metric:"fees",label:"User fees",status:"current",value:0,reason:"source observed",source_url:null,unit:"USD",period_end:now,received_at:now,max_age_seconds:100},
      {metric:"earnings",label:"Corporate earnings",status:"not_applicable",value:null,reason:"Not corporate equity",source_url:null},
      {metric:"emissions",label:"Emissions",status:"missing",value:null,reason:"No data",source_url:null},
      {metric:"old",label:"Old fees",status:"current",value:987654321,reason:"old",source_url:null,period_end:"2020-01-01T00:00:00Z",received_at:"2020-01-01T00:00:00Z",max_age_seconds:100},
    ]};
    const html=renderToStaticMarkup(createElement(AnalystContext,{fundamentals}));
    expect(html).toContain("0 USD"); expect(html).toContain("not applicable"); expect(html).toContain("missing");
    expect(html).not.toContain("987,654,321"); expect(html).toContain("not assessed");
  });
});
