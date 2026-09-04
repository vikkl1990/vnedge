import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchChartCandles,
  type CorrectionLane,
  type ScannerAuditEvent,
} from "../api";

vi.mock("@luxalgo/vela", () => ({ Vela: class Vela {} }));

import {
  bucketOpenMs,
  eventTimeMs,
  marketsFromLanes,
  toPlans,
  type MarketChoice,
} from "./ScannerChart";

const event = (overrides: Partial<ScannerAuditEvent> = {}): ScannerAuditEvent => ({
  lane: "delta_btc_15m",
  ts: "2026-09-04T12:07:03.000Z",
  bar_ts: "2026-09-04T12:00:00.000Z",
  kind: "entry",
  source_event: "shadow_intent",
  intent_key: "intent-1",
  strategy_id: "structure_bos_realtime_v2",
  exchange: "delta_india",
  symbol: "BTC/USD:USD",
  timeframe: "15m",
  side: "long",
  price: 100,
  entry_price: 100,
  stop_price: 99,
  approved: true,
  reason: "accepted",
  ...overrides,
});

const deltaMarket: MarketChoice = {
  key: "delta_india:BTC/USD:USD",
  exchange: "delta_india",
  symbol: "BTC/USD:USD",
  label: "BTC · DELTA INDIA",
};

describe("scanner chart evidence mapping", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("fails closed when lane inventory is unavailable", () => {
    expect(marketsFromLanes(undefined)).toEqual([]);
  });

  it("uses actual acceptance time for entries and structure time for signals", () => {
    expect(eventTimeMs(event())).toBe(Date.parse("2026-09-04T12:07:03.000Z"));
    expect(eventTimeMs(event({ kind: "signal" }))).toBe(
      Date.parse("2026-09-04T12:00:00.000Z"),
    );
  });

  it("buckets mid-bar acceptance onto the selected causal candle", () => {
    const actual = Date.parse("2026-09-04T12:07:03.000Z");
    expect(bucketOpenMs(actual, "15m")).toBe(
      Date.parse("2026-09-04T12:00:00.000Z"),
    );
    expect(toPlans([event()], deltaMarket, "15m", new Map())[0]).toMatchObject({
      event_ts_ms: actual,
      bar_ts_ms: Date.parse("2026-09-04T12:00:00.000Z"),
    });
  });

  it("keeps Delta USD and Binance USDT markets separate", () => {
    const lanes = [
      {
        lane_id: "delta_btc_15m",
        exchange: "delta_india",
        symbol: "BTC/USD:USD",
        observation_class: "shadow_observe",
      },
      {
        lane_id: "binance_btc_15m",
        exchange: "binanceusdm",
        symbol: "BTC/USDT:USDT",
        observation_class: "measurement",
      },
    ] as CorrectionLane[];
    const markets = marketsFromLanes(lanes);
    expect(markets.map((market) => market.key)).toEqual([
      "delta_india:BTC/USD:USD",
      "binanceusdm:BTC/USDT:USDT",
    ]);
    expect(toPlans([event()], markets[1], "15m", new Map())).toEqual([]);
  });

  it("requests the canonical Delta storage identity, not a slash path", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          symbol: "BTCUSD",
          timeframe: "15m",
          source: "canonical_lake",
          count: 0,
          truncated: false,
          candles: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await fetchChartCandles("BTC/USD:USD", "15m", 500, "delta_india");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/candles/BTCUSD?timeframe=15m&n=500&exchange=delta_india",
    );
  });

  it("recovers the event venue from its lane when journal rows omit exchange", () => {
    const withoutExchange = event({ exchange: undefined });
    const plans = toPlans(
      [withoutExchange],
      deltaMarket,
      "15m",
      new Map([["delta_btc_15m", "delta_india"]]),
    );
    expect(plans).toHaveLength(1);
  });
});
