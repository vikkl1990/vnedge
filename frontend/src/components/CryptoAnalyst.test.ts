import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { CryptoAnalystView, analystMatches, analystNumber, publicMarketValues, historyProgress, type AnalystMarket, type AnalystPayload } from "./CryptoAnalyst";

const row: AnalystMarket = { symbol: "BTCUSD", timeframe: "15m", state: "current", bias: "bullish", alignment: 65,
  coverage_pct: 100, analysis_id: "proof", as_of: "2026-09-13T12:00:00Z", supports: ["Price above the EMA stack"],
  conflicts: ["Volume does not confirm"], issues: [], components: [], metrics: { price: 100, return_12_pct: 0, session_vwap: null },
  sparkline: [98, 99, 100], setups: ["upside_breakout"] };
const payload: AnalystPayload = { schema: "crypto_analyst_v1", spec_hash: "spec", generated_at: new Date().toISOString(),
  exchange: "delta_india", timeframe: "15m", session: "Asia UTC 00–08", brief: "One current market; bullish profile.",
  universe: { scope: "local", discovered: 1, displayed: 1, current: 1, truncated: false },
  breadth: { bullish: 1, bearish: 0, mixed: 0, denominator: 1 }, markets: [row] };

describe("Crypto Analyst", () => {
  const publicRow = (): AnalystMarket => ({ ...row, state: "unavailable", bias: "unknown", metrics: {},
    alignment: null, as_of: null, components: [], supports: [], conflicts: [], setups: [], sparkline: [],
    issues: ["history_gap", "need_60_contiguous_bars"],
    history: { required_bars: 60, contiguous_bars: 1, status: "collecting", reason: "history_gap" },
    market_evidence: { conditions: { state: "current", source: "delta_public_rest_ticker",
      venue_ts: new Date(Date.now()-1000).toISOString(), received_at: new Date(Date.now()-500).toISOString(),
      expires_after_s: 120, values: { mark_price: 78506.12, spread_bps: 0.064, open_interest_usd: 72210873 } } } });
  it("shows public market data while technical history is unavailable without creating a score", () => {
    const market = publicRow();
    const html = renderToStaticMarkup(createElement(CryptoAnalystView, { data: { ...payload,
      markets: [market], universe: { ...payload.universe, current: 0 },
      breadth: { bullish: 0, bearish: 0, mixed: 0, denominator: 0 } } }));
    expect(html).toContain("78,506.12");
    expect(html).toContain("72,210,873");
    expect(html).toContain("delta_public_rest_ticker");
    expect(html).toContain("1 / 60 consecutive verified bars");
    expect(html).toContain("Market data and technical analysis are separate");
    expect(html).toContain("Public mark price");
    expect(html).toContain("Last closed price");
    expect(analystMatches(market, "", "trend_watch", "all")).toBe(false);
    expect(market.metrics).toEqual({});
  });
  it("withholds expired, future, unproven and disconnected public values", () => {
    const market = publicRow();
    expect(publicMarketValues(market, Date.now()).mark_price).toBe(78506.12);
    expect(publicMarketValues(market, Date.now()+121000)).toEqual({});
    expect(publicMarketValues(market, Date.now()-10000)).toEqual({});
    expect(publicMarketValues(market, Date.now(), true)).toEqual({});
    delete market.market_evidence!.conditions.expires_after_s;
    expect(publicMarketValues(market, Date.now())).toEqual({});
  });
  it("does not pretend product discovery is candle coverage", () => {
    expect(historyProgress({ ...publicRow(), issues: ["no_canonical_analysis_in_covered_universe"] }))
      .toBe("Canonical history not collected");
    expect(historyProgress({ ...publicRow(), history: { required_bars: 60, contiguous_bars: 0,
      status: "unverified", reason: "closed_bar_proof_missing" } })).toContain("History unverified");
  });
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
