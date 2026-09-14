import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DossierFacts, MarketConditions, observationState, type Dossier } from "./AnalystEvidence";

describe("Analyst evidence presentation", () => {
  it("hides expired numbers, not merely an amber badge", () => {
    const html = renderToStaticMarkup(createElement(MarketConditions, { observations: {
      conditions: { state: "stale", values: { spread_bps: 987.65, indicative_funding_pct: 432.1 } },
      flow: { state: "stale", buy_share_pct: 99.123 },
    } }));
    expect(html).not.toContain("987.65"); expect(html).not.toContain("432.1"); expect(html).not.toContain("99.123");
    expect(html).toContain("Unavailable"); expect(html).toContain("not lane BBO");
  });
  it("does not hide real zero observations or imply settled funding", () => {
    const clock = { venue_ts: new Date().toISOString(), received_at: new Date().toISOString(), expires_after_s: 120 };
    const html = renderToStaticMarkup(createElement(MarketConditions, { observations: {
      conditions: { ...clock, state: "current", values: { open_interest_contracts: 0, indicative_funding_pct: 0 } },
      flow: { ...clock, state: "current", sample_trades: 50, buy_share_pct: 42 },
    } }));
    expect(html).toContain(">0</dd>"); expect(html).toContain("incomplete coverage");
    expect(html).toContain("not settled cash"); expect(html).toContain("No institution identity inferred");
  });
  it("expires a cached current response when updates stop and refuses missing clocks", () => {
    const stamp = "2026-09-14T00:00:00Z", now = Date.parse(stamp);
    const obs = { state: "current", venue_ts: stamp, received_at: stamp, expires_after_s: 120 };
    expect(observationState(obs, now + 119_000)).toBe("current");
    expect(observationState(obs, now + 121_000)).toBe("stale");
    expect(observationState(obs, now - 1)).toBe("stale");
    expect(observationState({ state: "current" }, now)).toBe("unavailable");
  });
  it("keeps disagreement, source times and evidence hashes visible", () => {
    const data: Dossier = { dossier_id: "id", generated_at: new Date().toISOString(), symbol: "BTCUSD", frames: [], market_evidence: {}, gaps: [],
      conflicts: ["timeframe_direction_disagreement"], clock_note: "Separate closed clocks", evidence: [{ id: "source-hash", kind: "conditions", as_of: null, state: "unavailable", source: "delta_public_rest_ticker", summary: "Book unavailable" }] };
    const html = renderToStaticMarkup(createElement(DossierFacts, { data }));
    expect(html).toContain("timeframe direction disagreement"); expect(html).toContain("source-hash");
    expect(html).toContain("No source time"); expect(html).not.toContain("VALID EDGE");
  });
});
