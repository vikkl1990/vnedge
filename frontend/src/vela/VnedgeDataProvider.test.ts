import { describe, expect, it, vi } from "vitest";
import type { ChartCandles } from "../api";
import {
  canonicalChartSymbol,
  VnedgeDataProvider,
} from "./VnedgeDataProvider";

const payload = (candles: ChartCandles["candles"]): ChartCandles => ({
  symbol: "BTCUSD",
  timeframe: "15m",
  source: "canonical_lake",
  count: candles.length,
  truncated: false,
  candles,
});
describe("VNEDGE Vela provider", () => {
  it("normalizes venue-native symbols without merging USD and USDT", () => {
    expect(canonicalChartSymbol("BTC/USD:USD")).toBe("BTCUSD");
    expect(canonicalChartSymbol("BTC/USDT:USDT")).toBe("BTCUSDT");
  });

  it("returns ordered unique epoch-ms bars and forwards Vela's bounded range", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      payload([
        { time: 2, open: 2, high: 3, low: 1, close: 2, volume: 2 },
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
      { fromMs: 1_000, toMs: 2_000 },
    );
    expect(bars.map((bar) => bar.time)).toEqual([1_000, 2_000]);
    expect(bars[1].close).toBe(3);
    provider.destroy();
  });
});
