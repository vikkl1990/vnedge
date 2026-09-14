import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { CryptoAnalystView, analystMatches, analystNumber, type AnalystMarket, type AnalystPayload } from "./CryptoAnalyst";

const row: AnalystMarket = { symbol: "BTCUSD", timeframe: "15m", state: "current", bias: "bullish", alignment: 65,
  coverage_pct: 100, analysis_id: "proof", as_of: "2026-09-13T12:00:00Z", supports: ["Price above the EMA stack"],
  conflicts: ["Volume does not confirm"], issues: [], components: [], metrics: { price: 100, return_12_pct: 0, session_vwap: null },
  sparkline: [98, 99, 100], setups: ["upside_breakout"] };
const payload: AnalystPayload = { schema: "crypto_analyst_v1", spec_hash: "spec", generated_at: new Date().toISOString(),
  exchange: "delta_india", timeframe: "15m", session: "Asia UTC 00–08", brief: "One current market; bullish profile.",
  universe: { scope: "local", discovered: 1, displayed: 1, current: 1, truncated: false },
  breadth: { bullish: 1, bearish: 0, mixed: 0, denominator: 1 }, markets: [row] };

describe("Crypto Analyst", () => {
  it("preserves zero and unavailable values", () => {
    expect(analystNumber(0)).toBe("0");
    expect(analystNumber(null)).toBe("—");
    expect(analystNumber(Infinity)).toBe("—");
  });
  it("provides an honest empty workspace, not invented opportunities", () => {
    const html = renderToStaticMarkup(createElement(CryptoAnalystView));
    expect(html).toContain("Crypto Analyst");
    expect(html).toContain("Awaiting coverage");
    expect(html).toContain("No matching observations");
    expect(html).not.toContain("VALID EDGE");
  });
  it("shows support, opposition and non-probability ranking", () => {
    const html = renderToStaticMarkup(createElement(CryptoAnalystView, { data: payload }));
    expect(html).toContain("Volume does not confirm");
    expect(html).toContain("Price above the EMA stack");
    expect(html).toContain("not probability");
    expect(html).toContain("NO ORDER ACCESS");
    expect(html).toContain("not an LLM forecast");
  });
  it("does not rank stale observations as current scan matches", () => {
    expect(analystMatches(row, "btc", "upside_breakout", "bullish")).toBe(true);
    expect(analystMatches({ ...row, state: "stale" }, "", "upside_breakout", "all")).toBe(false);
    expect(analystMatches(row, "eth", "all", "all")).toBe(false);
  });
  it("shows connection failures even with old data", () => {
    const html = renderToStaticMarkup(createElement(CryptoAnalystView, { data: payload, error: true }));
    expect(html).toContain("Connection unavailable");
    expect(html).toContain("previous report below is historical");
  });
});
