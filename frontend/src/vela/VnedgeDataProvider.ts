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
  type ChartCandles,
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
    if (candle.source !== "canonical_tick_lake" || !candle.identity_ok) {
      throw new Error("chart_source_or_identity_unverified");
    }
    const bar = toVelaBar(candle);
    if (
      Number.isFinite(bar.time) &&
      Number.isFinite(bar.open) &&
      Number.isFinite(bar.high) &&
      Number.isFinite(bar.low) &&
      Number.isFinite(bar.close)
    ) {
      const previous = byTime.get(bar.time);
      if (previous && barSignature(previous) !== barSignature(bar)) {
        throw new Error("chart_conflicting_duplicate");
      }
      byTime.set(bar.time, bar);
    } else {
      throw new Error("chart_ohlc_nonfinite");
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
  lastTime: number | null;
}

export interface ChartFeedState {
  status: "LOADING" | "CLOSED" | "WATCH" | "UNVERIFIED" | "STALE" | "ERROR" | "EMPTY" | "RELOADING";
  reason: string;
  lastSuccessAt: number | null;
  lastBarTime: number | null;
  gapSlots: number;
  excludedSources: Record<string, number>;
}

const TF_MS: Record<ChartTimeframe, number> = {
  "1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000,
};

export function chartFeedState(payload: ChartCandles, now = Date.now()): ChartFeedState {
  const bars = payload.candles;
  const last = bars[bars.length - 1];
  const width = TF_MS[chartTimeframe(payload.timeframe)];
  const gapSlots = bars.slice(1).reduce((n, b, i) => n + Math.max(0, (b.time - bars[i].time) * 1000 / width - 1), 0);
  const unverified = bars.some(b => b.proof_state === "UNVERIFIED" || !b.proof_state);
  const stale = last && now - (last.close_time ?? last.time) * 1000 > width + 30_000;
  const status = stale ? "STALE" : unverified ? "UNVERIFIED" : last?.proof_state ?? (payload.status === "UNVERIFIED" ? "UNVERIFIED" : "EMPTY");
  return { status, reason: gapSlots ? `${gapSlots} missing bar slots; not interpolated` :
    stale ? "no recent lake close" : status === "UNVERIFIED" ? "persisted proof missing or invalid" : "lake only · display, not trading readiness",
    lastSuccessAt: now, lastBarTime: last ? last.time * 1000 : null,
    gapSlots, excludedSources: payload.excluded_sources ?? {} };
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
  private destroyed = false;
  private invalidated = false;
  private revisions = new Map<string, { hash: string; cutoff: number | null }>();
  private latest = new Map<string, number>();
  private state: ChartFeedState = { status: "LOADING", reason: "loading lake", lastSuccessAt: null, lastBarTime: null, gapSlots: 0, excludedSources: {} };

  constructor(
    readonly market: VnedgeProviderMarket,
    private readonly fetcher: CandleFetcher = fetchChartCandles,
    private readonly pollMs = 10_000,
    private readonly hooks: {
      onState?: (state: ChartFeedState) => void;
      onRevision?: () => void;
    } = {},
  ) {}

  private notify(state: ChartFeedState) {
    if (this.destroyed) return;
    this.state = state;
    this.hooks.onState?.(state);
  }

  private accept(payload: ChartCandles, tf: string, latestRead: boolean,
                 baseline?: { hash: string; cutoff: number | null }) {
    if (this.destroyed || this.invalidated) throw new Error("chart_load_superseded");
    if (payload.status === "ERROR") throw new Error("chart_store_read_failed");
    if (payload.exchange !== this.market.exchange || payload.timeframe !== tf ||
        canonicalChartSymbol(payload.symbol) !== canonicalChartSymbol(this.market.symbol) ||
        payload.source_policy !== "canonical_tick_lake_only" || !payload.series_revision) {
      throw new Error("chart_series_identity_missing_or_mismatch");
    }
    const prior = this.revisions.get(tf);
    if (baseline && payload.previous_revision !== null && payload.previous_revision !== undefined &&
        baseline.hash !== payload.previous_revision) {
      this.invalidated = true;
      this.notify({ ...this.state, status: "RELOADING", reason: "lake revision changed; discarding chart cache" });
      this.hooks.onRevision?.();
      throw new Error("chart_revision_changed");
    }
    // Older pages do not move a live poll's baseline backward.
    if (!prior || (latestRead && (payload.revision_cutoff_ms ?? -Infinity) >= (prior.cutoff ?? -Infinity)))
      this.revisions.set(tf, { hash: payload.series_revision, cutoff: payload.revision_cutoff_ms ?? null });
    const bars = orderedUnique(payload.candles);
    if (bars.length) this.latest.set(tf, Math.max(this.latest.get(tf) ?? -Infinity, bars[bars.length - 1].time));
    if (latestRead || !prior) this.notify(chartFeedState(payload));
    return bars;
  }

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
    const baseline = this.revisions.get(timeframe);
    const payload = await this.fetcher(
      this.market.symbol,
      chartTimeframe(timeframe),
      range.limit ?? 500,
      this.market.exchange,
      { fromMs: range.from, toMs: range.to,
        revisionBeforeMs: baseline?.cutoff ?? undefined },
    );
    return this.accept(payload, timeframe, range.to === undefined, baseline);
  }

  subscribe(
    ticker: string,
    timeframe: string,
    onBar: (bar: OHLCV) => void,
  ): () => void {
    const tf = chartTimeframe(timeframe);
    if (canonicalChartSymbol(ticker) !== canonicalChartSymbol(this.market.symbol)) {
      throw new Error("chart_subscription_market_mismatch");
    }
    const key = `${canonicalChartSymbol(ticker)}:${tf}`;
    let subscription = this.subscriptions.get(key);
    if (!subscription) {
      subscription = {
        callbacks: new Set(),
        timer: null,
        inFlight: false,
        lastSignature: "",
        lastTime: this.latest.get(tf) ?? null,
      };
      this.subscriptions.set(key, subscription);
    }
    subscription.callbacks.add(onBar);

    const poll = async () => {
      const current = this.subscriptions.get(key);
      if (this.destroyed || this.invalidated || !current || current.callbacks.size === 0) return;
      if (this.state.lastSuccessAt && Date.now() - this.state.lastSuccessAt > 30_000) {
        this.notify({ ...this.state, status: "STALE", reason: "lake poll has not completed for 30s" });
      }
      if (current.inFlight) return;
      current.inFlight = true;
      try {
        const baseline = this.revisions.get(tf);
        const payload = await this.fetcher(
          this.market.symbol,
          tf,
          5000,
          this.market.exchange,
          { fromMs: current.lastTime ?? undefined,
            revisionBeforeMs: baseline?.cutoff ?? undefined },
        );
        if (this.destroyed || this.subscriptions.get(key) !== current) return;
        const bars = this.accept(payload, tf, true, baseline);
        if (payload.truncated && current.lastTime !== null) {
          this.invalidated = true;
          this.notify({ ...this.state, status: "RELOADING", reason: "chart_catchup_window_exceeded" });
          this.hooks.onRevision?.();
          throw new Error("chart_catchup_window_exceeded");
        }
        for (const bar of bars) {
          if (current.lastTime !== null && bar.time < current.lastTime) continue;
          const signature = barSignature(bar);
          if (signature === current.lastSignature) continue;
          current.lastSignature = signature;
          current.lastTime = bar.time;
          for (const callback of current.callbacks) callback(bar);
        }
      } catch (error) {
        if (!this.invalidated) this.notify({ ...this.state, status: "ERROR",
          reason: error instanceof Error ? error.message : "chart_poll_failed" });
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
    this.destroyed = true;
    for (const subscription of this.subscriptions.values()) {
      if (subscription.timer !== null) clearInterval(subscription.timer);
    }
    this.subscriptions.clear();
  }
}
