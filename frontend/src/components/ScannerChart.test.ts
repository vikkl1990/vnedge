import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchChartCandles,
  fetchMechanismContext,
  type CorrectionLane,
  type ScannerAuditEvent,
} from "../api";

vi.mock("@luxalgo/vela", () => ({ Vela: class Vela {} }));

import {
  bucketOpenMs,
  eventTimeMs,
  activeSessionWindows,
  lifecycleSummary,
  marketsFromLanes,
  toEventMarkers,
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

  it("passes a bounded provider range and canonicalizes context paths", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ candles: [], ready: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    await fetchChartCandles("BTC/USD:USD", "15m", 50, "delta_india", {
      fromMs: 1000,
      toMs: 2000,
    });
    expect(fetchMock.mock.calls[0][0]).toContain("from_ms=1000");
    expect(fetchMock.mock.calls[0][0]).toContain("to_ms=2000");
    await fetchMechanismContext("BTC/USD:USD", "15m", "delta_india");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/candles/BTCUSD/context?timeframe=15m&exchange=delta_india",
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

  it("keeps an honest journal-tail lifecycle instead of treating fires as the funnel", () => {
    const events = [
      event({ kind: "evaluation", approved: false }),
      event({ kind: "signal", approved: true, ts: "2026-09-04T12:08:00Z" }),
      event({ kind: "rejection", approved: false, ts: "2026-09-04T12:09:00Z" }),
      event({ kind: "entry", approved: true, ts: "2026-09-04T12:10:00Z" }),
      event({ kind: "exit", approved: true, ts: "2026-09-04T12:11:00Z" }),
    ];
    const markers = toEventMarkers(events, deltaMarket, "15m", new Map());
    expect(lifecycleSummary(markers)).toEqual({
      evaluations: 1,
      signals: 1,
      accepted: 1,
      rejected: 1,
      exits: 1,
    });
  });

  it("groups the 12-16 UTC research session into display-only daily bands", () => {
    const bars = [11, 12, 13, 16].map((hour) => ({
      time: Date.parse(`2026-09-04T${String(hour).padStart(2, "0")}:00:00Z`),
      open: 100,
      high: 100 + hour,
      low: 100 - hour,
      close: 100,
      volume: 1,
    }));
    expect(activeSessionWindows(bars, "1h")).toEqual([
      {
        start: Date.parse("2026-09-04T12:00:00Z"),
        end: Date.parse("2026-09-04T14:00:00Z"),
        low: 87,
        high: 113,
      },
    ]);
  });
});
