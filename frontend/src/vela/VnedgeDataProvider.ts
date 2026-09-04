import type {
  BarRange,
  DataProvider,
  OHLCV,
  ProviderInfo,
  SymbolDescriptor,
  SymbolInfo,
} from "@luxalgo/vela";
import {
  fetchChartCandles,
  type ChartCandle,
  type ChartTimeframe,
} from "../api";

export interface VnedgeProviderMarket {
  exchange: string;
  symbol: string;
  label: string;
}

export type CandleFetcher = typeof fetchChartCandles;

const SUPPORTED_TIMEFRAMES: readonly ChartTimeframe[] = [
  "1m",
  "5m",
  "15m",
  "1h",
  "4h",
];

export const canonicalChartSymbol = (raw: string) =>
  raw.split(":", 1)[0].replace(/[^A-Za-z0-9]/g, "").toUpperCase();

function chartTimeframe(raw: string): ChartTimeframe {
  if ((SUPPORTED_TIMEFRAMES as readonly string[]).includes(raw)) {
    return raw as ChartTimeframe;
  }
  throw new Error(`unsupported canonical chart timeframe: ${raw}`);
}

function toVelaBar(candle: ChartCandle): OHLCV {
  return {
    time: candle.time * 1_000,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
    volume: candle.volume,
  };
}

function orderedUnique(candles: ChartCandle[]): OHLCV[] {
  const byTime = new Map<number, OHLCV>();
  for (const candle of candles) {
    const bar = toVelaBar(candle);
    if (
      Number.isFinite(bar.time) &&
      Number.isFinite(bar.open) &&
      Number.isFinite(bar.high) &&
      Number.isFinite(bar.low) &&
      Number.isFinite(bar.close)
    ) {
      byTime.set(bar.time, bar);
    }
  }
  return [...byTime.values()].sort((left, right) => left.time - right.time);
}

const barSignature = (bar: OHLCV) =>
  [bar.time, bar.open, bar.high, bar.low, bar.close, bar.volume ?? 0].join(":");

interface Subscription {
  callbacks: Set<(bar: OHLCV) => void>;
  timer: ReturnType<typeof setInterval> | null;
  inFlight: boolean;
  lastSignature: string;
}

/**
 * Vela adapter for the VNEDGE canonical candle lake.
 *
 * This is a presentation adapter only. It reads the same immutable closed bars
 * as research/shadow, and it never publishes data back into a scanner. A small
 * tail poll supplies newly closed bars without replacing the whole chart.
 */
export class VnedgeDataProvider implements DataProvider {
  private readonly subscriptions = new Map<string, Subscription>();

  constructor(
    readonly market: VnedgeProviderMarket,
    private readonly fetcher: CandleFetcher = fetchChartCandles,
    private readonly pollMs = 10_000,
  ) {}

  info(): ProviderInfo {
    return {
      name: "vnedge",
      displayName: "VNEDGE canonical lake",
      supportedTimeframes: SUPPORTED_TIMEFRAMES,
      capabilities: { enumerate: true, stream: true, symbolInfo: true },
    };
  }

  async listSymbols(): Promise<SymbolDescriptor[]> {
    return [
      {
        ticker: canonicalChartSymbol(this.market.symbol),
        description: this.market.label,
        type: "perpetual",
      },
    ];
  }

  async getSymbolInfo(ticker: string): Promise<SymbolInfo | undefined> {
    const canonical = canonicalChartSymbol(ticker);
    if (canonical !== canonicalChartSymbol(this.market.symbol)) return undefined;
    const tick = canonical.startsWith("BTC")
      ? 0.5
      : canonical.startsWith("ETH")
        ? 0.05
        : 0.01;
    return {
      ticker: canonical,
      description: this.market.label,
      type: "perpetual",
      timezone: "Etc/UTC",
      session: "24x7",
      mintick: tick,
      pricescale: Math.round(1 / tick),
    };
  }

  async getBars(
    ticker: string,
    timeframe: string,
    range: BarRange,
  ): Promise<OHLCV[]> {
    const requested = canonicalChartSymbol(ticker);
    const expected = canonicalChartSymbol(this.market.symbol);
    if (requested !== expected) return [];
    const payload = await this.fetcher(
      this.market.symbol,
      chartTimeframe(timeframe),
      range.limit ?? 500,
      this.market.exchange,
      { fromMs: range.from, toMs: range.to },
    );
    return orderedUnique(payload.candles);
  }

  subscribe(
    ticker: string,
    timeframe: string,
    onBar: (bar: OHLCV) => void,
  ): () => void {
    const tf = chartTimeframe(timeframe);
    const key = `${canonicalChartSymbol(ticker)}:${tf}`;
    let subscription = this.subscriptions.get(key);
    if (!subscription) {
      subscription = {
        callbacks: new Set(),
        timer: null,
        inFlight: false,
        lastSignature: "",
      };
      this.subscriptions.set(key, subscription);
    }
    subscription.callbacks.add(onBar);

    const poll = async () => {
      const current = this.subscriptions.get(key);
      if (!current || current.inFlight || current.callbacks.size === 0) return;
      current.inFlight = true;
      try {
        const payload = await this.fetcher(
          this.market.symbol,
          tf,
          3,
          this.market.exchange,
        );
        const bars = orderedUnique(payload.candles);
        const latest = bars[bars.length - 1];
        if (!latest) return;
        const signature = barSignature(latest);
        if (signature === current.lastSignature) return;
        current.lastSignature = signature;
        for (const callback of current.callbacks) callback(latest);
      } catch {
        // A read-only chart poll must never affect lane health or scanner state.
      } finally {
        current.inFlight = false;
      }
    };

    if (subscription.timer === null) {
      void poll();
      subscription.timer = setInterval(() => void poll(), this.pollMs);
    }

    return () => {
      const current = this.subscriptions.get(key);
      if (!current) return;
      current.callbacks.delete(onBar);
      if (current.callbacks.size === 0) {
        if (current.timer !== null) clearInterval(current.timer);
        this.subscriptions.delete(key);
      }
    };
  }

  destroy() {
    for (const subscription of this.subscriptions.values()) {
      if (subscription.timer !== null) clearInterval(subscription.timer);
    }
    this.subscriptions.clear();
  }
}
