import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChartCandles } from "../api";
import {
  canonicalChartSymbol,
  VnedgeDataProvider,
  chartFeedState,
} from "./VnedgeDataProvider";

const payload = (candles: ChartCandles["candles"]): ChartCandles => ({
  symbol: "BTCUSD",
  timeframe: "15m",
  source: "canonical_lake",
  count: candles.length,
  truncated: false,
  exchange: "delta_india",
  status: "OK",
  source_policy: "canonical_tick_lake_only",
  series_revision: "revision-1",
  revision_cutoff_ms: candles.length ? candles[candles.length - 1].time * 1000 : null,
  candles: candles.map(c => ({ ...c, source: "canonical_tick_lake", identity_ok: true, is_closed: true,
    hash_valid: true, content_sha256: `hash-${c.time}`, proof_state: "CLOSED", close_time: c.time + 900 })),
});
describe("VNEDGE Vela provider", () => {
  afterEach(() => vi.useRealTimers());
  it("normalizes venue-native symbols without merging USD and USDT", () => {
    expect(canonicalChartSymbol("BTC/USD:USD")).toBe("BTCUSD");
    expect(canonicalChartSymbol("BTC/USDT:USDT")).toBe("BTCUSDT");
  });

  it("returns ordered unique epoch-ms bars and forwards Vela's bounded range", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      payload([
        { time: 2, open: 2, high: 4, low: 1, close: 3, volume: 3 },
        { time: 1, open: 1, high: 2, low: 1, close: 1, volume: 1 },
        { time: 2, open: 2, high: 4, low: 1, close: 3, volume: 3 },
      ]),
    );
    const provider = new VnedgeDataProvider(
      { exchange: "delta_india", symbol: "BTC/USD:USD", label: "BTC · DELTA" },
      fetcher,
    );
    const bars = await provider.getBars("BTCUSD", "15m", {
      from: 1_000,
      to: 2_000,
      limit: 50,
    });
    expect(fetcher).toHaveBeenCalledWith(
      "BTC/USD:USD",
      "15m",
      50,
      "delta_india",
      { fromMs: 1_000, toMs: 2_000, revisionBeforeMs: undefined },
    );
    expect(bars.map((bar) => bar.time)).toEqual([1_000, 2_000]);
    expect(bars[1].close).toBe(3);
    provider.destroy();
  });

  const market = { exchange: "delta_india", symbol: "BTC/USD:USD", label: "BTC" };
  const base = Date.parse("2026-09-01T12:00:00Z") / 1000;
  const bar = (i: number) => ({ time: base + i * 900, open: 1, high: 2, low: 1, close: 2, volume: 1 });

  it("emits every intervening bar after a missed poll, never inventing gaps", async () => {
    vi.useFakeTimers();
    vi.setSystemTime((base + 5 * 900) * 1000);
    const fetcher = vi.fn().mockResolvedValueOnce(payload([bar(0)]))
      .mockResolvedValue({ ...payload([bar(0), bar(1), bar(3)]), previous_revision: "revision-1" });
    const state = vi.fn();
    const provider = new VnedgeDataProvider(market, fetcher, 1000, { onState: state });
    await provider.getBars("BTCUSD", "15m", {});
    const callback = vi.fn();
    const off = provider.subscribe("BTCUSD", "15m", callback);
    await vi.advanceTimersByTimeAsync(0);
    expect(callback.mock.calls.map(c => c[0].time)).toEqual([bar(0).time, bar(1).time, bar(3).time].map(t => t * 1000));
    expect(state.mock.calls[state.mock.calls.length - 1][0].gapSlots).toBe(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(callback).toHaveBeenCalledTimes(3);
    off(); provider.destroy();
  });

  it("signals repair invalidation without emitting the changed cached bar", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValueOnce(payload([bar(0)]))
      .mockResolvedValue({ ...payload([bar(0)]), previous_revision: "repaired-prefix" });
    const onRevision = vi.fn();
    const provider = new VnedgeDataProvider(market, fetcher, 1000, { onRevision });
    await provider.getBars("BTCUSD", "15m", {});
    const callback = vi.fn();
    provider.subscribe("BTCUSD", "15m", callback);
    await vi.advanceTimersByTimeAsync(1000);
    expect(onRevision).toHaveBeenCalledTimes(1);
    expect(callback).not.toHaveBeenCalled();
    provider.destroy();
  });

  it("reports errors and ignores a response after unsubscribe/destroy", async () => {
    vi.useFakeTimers();
    const state = vi.fn();
    const fetcher = vi.fn().mockRejectedValue(new Error("lake offline"));
    const provider = new VnedgeDataProvider(market, fetcher, 1000, { onState: state });
    const callback = vi.fn();
    const off = provider.subscribe("BTCUSD", "15m", callback);
    await vi.advanceTimersByTimeAsync(0);
    expect(state.mock.calls[0][0].status).toBe("ERROR");
    let release!: (value: ChartCandles) => void;
    fetcher.mockImplementation(() => new Promise<ChartCandles>(resolve => { release = resolve; }));
    await vi.advanceTimersByTimeAsync(1000);
    off(); provider.destroy(); release(payload([bar(0)]));
    await vi.advanceTimersByTimeAsync(0);
    expect(callback).not.toHaveBeenCalled();
  });

  it("visibly reloads rather than emitting an incomplete catch-up", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValueOnce(payload([bar(0)]))
      .mockResolvedValue({ ...payload([bar(3)]), previous_revision: "revision-1", truncated: true });
    const state = vi.fn();
    const onRevision = vi.fn();
    const provider = new VnedgeDataProvider(market, fetcher, 1000, { onState: state, onRevision });
    await provider.getBars("BTCUSD", "15m", {});
    const callback = vi.fn();
    provider.subscribe("BTCUSD", "15m", callback);
    await vi.advanceTimersByTimeAsync(0);
    expect(state.mock.calls[state.mock.calls.length - 1][0].status).toBe("RELOADING");
    expect(onRevision).toHaveBeenCalledTimes(1);
    expect(callback).not.toHaveBeenCalled();
    provider.destroy();
  });

  it("does not accept official or wrong-market data under a canonical key", async () => {
    const bad = payload([bar(0)]);
    bad.candles[0].source = "official_delta_ohlc";
    const provider = new VnedgeDataProvider(market, vi.fn().mockResolvedValue(bad));
    await expect(provider.getBars("BTCUSD", "15m", {})).rejects.toThrow("source_or_identity");
    expect(() => provider.subscribe("ETHUSD", "15m", () => {})).toThrow("market_mismatch");
    provider.destroy();
  });

  it("labels closed DTOs CLOSED despite Vela treating the newest bar as forming", () => {
    const good = payload([bar(0)]);
    expect(chartFeedState(good, (base + 901) * 1000).status).toBe("CLOSED");
    expect(chartFeedState(good, (base + 4000) * 1000).status).toBe("STALE");
    good.candles[0].proof_state = "UNVERIFIED";
    expect(chartFeedState(good, (base + 901) * 1000).status).toBe("UNVERIFIED");
    good.candles[0].proof_state = "WATCH";
    expect(chartFeedState(good, (base + 100) * 1000).status).toBe("WATCH");
  });
});
