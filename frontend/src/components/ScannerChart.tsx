// Scanner chart: canonical candles rendered by Vela (own-bars, fully offline)
// with scanner lifecycle events read from the same journal used by Desk.
// Presentation only: this component never supplies market data to a lane.

import { useEffect, useMemo, useRef, useState } from "react";
import { Vela } from "@luxalgo/vela";
import { useQuery } from "@tanstack/react-query";
import {
  fetchChartCandles,
  fetchMechanismContext,
  type ChartTimeframe,
  type CorrectionLane,
  type ScannerAuditEvent,
} from "../api";
import { useJournal, useLanes } from "../queries";
import { TerminalBadge, TerminalPanel } from "./Terminal";

const TIMEFRAMES: ChartTimeframe[] = ["5m", "15m", "1h", "4h"];
const OVERLAY_PLANS = 3;

export interface MarketChoice {
  key: string;
  exchange: string;
  symbol: string;
  label: string;
}

const TF_MS: Record<ChartTimeframe, number> = {
  "1m": 60_000,
  "5m": 5 * 60_000,
  "15m": 15 * 60_000,
  "1h": 60 * 60_000,
  "4h": 4 * 60 * 60_000,
};

const COLORS = {
  entry: "#60a5fa",
  stop: "#f87171",
  target: "#34d399",
  long: "#34d399",
  short: "#f87171",
  swing: "#eab308",
  channel: "#64748b",
};

type ChartInstance = InstanceType<typeof Vela>;

interface Plan {
  key: string;
  event_ts_ms: number;
  bar_ts_ms: number;
  side: string;
  entry: number;
  stop: number;
  target: number | null;
  reason: string;
  strategy_id: string;
  kind: "signal" | "entry";
}

interface EventMarker {
  key: string;
  bar_ts_ms: number;
  side: string;
  kind: "signal" | "entry" | "exit";
  event_price: number | null;
}

interface CandleBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

const canonicalSymbol = (raw: string) =>
  raw.split(":", 1)[0].replace(/[^A-Za-z0-9]/g, "").toUpperCase();

const baseAsset = (raw: string) => {
  const canonical = canonicalSymbol(raw);
  if (canonical.startsWith("BTC")) return "BTC";
  if (canonical.startsWith("ETH")) return "ETH";
  return canonical.slice(0, 6);
};

export function marketsFromLanes(lanes: CorrectionLane[] | undefined): MarketChoice[] {
  if (!lanes?.length) return [];
  const unique = new Map<string, MarketChoice>();
  const ordered = [...lanes].sort((a, b) => {
    const aPriority = a.observation_class === "shadow_observe" ? 0 : 1;
    const bPriority = b.observation_class === "shadow_observe" ? 0 : 1;
    return aPriority - bPriority;
  });
  for (const lane of ordered) {
    if (!lane.exchange || !lane.symbol) continue;
    const asset = baseAsset(lane.symbol);
    if (asset !== "BTC" && asset !== "ETH") continue;
    const key = `${lane.exchange}:${lane.symbol}`;
    if (unique.has(key)) continue;
    unique.set(key, {
      key,
      exchange: lane.exchange,
      symbol: lane.symbol,
      label: `${asset} · ${lane.exchange.replace(/_/g, " ").toUpperCase()}`,
    });
  }
  return [...unique.values()];
}

function laneExchangeMap(lanes: CorrectionLane[] | undefined) {
  return new Map((lanes ?? []).map((lane) => [lane.lane_id, lane.exchange]));
}

function eventMatchesMarket(
  event: ScannerAuditEvent,
  market: MarketChoice,
  laneExchanges: Map<string, string>,
) {
  if (canonicalSymbol(event.symbol) !== canonicalSymbol(market.symbol)) return false;
  const eventExchange = event.exchange || laneExchanges.get(event.lane);
  return !eventExchange || eventExchange === market.exchange;
}

export function eventTimeMs(event: ScannerAuditEvent) {
  if (event.kind === "entry") return Date.parse(event.entry_ts || event.ts);
  if (event.kind === "exit") return Date.parse(event.ts);
  return Date.parse(event.bar_ts || event.ts);
}

export function bucketOpenMs(timestamp: number, timeframe: ChartTimeframe) {
  const width = TF_MS[timeframe];
  return Math.floor(timestamp / width) * width;
}

export function toPlans(
  events: ScannerAuditEvent[] | undefined,
  market: MarketChoice,
  timeframe: ChartTimeframe,
  laneExchanges: Map<string, string>,
): Plan[] {
  if (!events) return [];
  const plans: Plan[] = [];
  for (const event of events) {
    if (!eventMatchesMarket(event, market, laneExchanges)) continue;
    if (event.timeframe && event.timeframe !== timeframe) continue;
    if (event.kind !== "signal" && event.kind !== "entry") continue;
    const entry = event.entry_price ?? event.decision_price ?? event.price;
    const stop = event.stop_price;
    if (typeof entry !== "number" || typeof stop !== "number") continue;
    const eventTs = eventTimeMs(event);
    if (!Number.isFinite(eventTs)) continue;
    plans.push({
      key: `${event.lane}:${event.intent_key || event.ts}:${event.kind}`,
      event_ts_ms: eventTs,
      bar_ts_ms: bucketOpenMs(eventTs, timeframe),
      side: event.side,
      entry,
      stop,
      target: typeof event.target_price === "number" ? event.target_price : null,
      reason: event.reason,
      strategy_id: event.strategy_id,
      kind: event.kind,
    });
  }
  plans.sort((a, b) => b.event_ts_ms - a.event_ts_ms);
  return plans;
}

function toEventMarkers(
  events: ScannerAuditEvent[] | undefined,
  market: MarketChoice,
  timeframe: ChartTimeframe,
  laneExchanges: Map<string, string>,
): EventMarker[] {
  if (!events) return [];
  const markers: EventMarker[] = [];
  for (const event of events) {
    if (!eventMatchesMarket(event, market, laneExchanges)) continue;
    if (event.timeframe && event.timeframe !== timeframe) continue;
    if (event.kind !== "signal" && event.kind !== "entry" && event.kind !== "exit") {
      continue;
    }
    if (event.kind === "entry" && !event.approved) continue;
    const eventTs = eventTimeMs(event);
    if (!Number.isFinite(eventTs)) continue;
    const eventPrice = event.entry_price ?? event.decision_price ?? event.price;
    markers.push({
      key: `${event.lane}:${event.intent_key || event.ts}:${event.kind}`,
      bar_ts_ms: bucketOpenMs(eventTs, timeframe),
      side: event.side,
      kind: event.kind,
      event_price: typeof eventPrice === "number" ? eventPrice : null,
    });
  }
  return markers;
}

function clearDrawings(chart: ChartInstance, ids: string[]) {
  for (const id of ids) {
    try {
      chart.drawings?.remove?.(id);
    } catch {
      // Vela may already have removed drawings during a market replacement.
    }
  }
}

const price = (value: number | null | undefined) =>
  typeof value === "number" && Number.isFinite(value)
    ? new Intl.NumberFormat("en-US", {
        maximumFractionDigits: value >= 1_000 ? 2 : 4,
      }).format(value)
    : "—";

const riskBps = (plan: Plan) =>
  plan.entry > 0 ? Math.abs((plan.entry - plan.stop) / plan.entry) * 1e4 : null;

const errorText = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

export function ScannerChart() {
  const lanes = useLanes();
  const markets = useMemo(() => marketsFromLanes(lanes.data?.lanes), [lanes.data]);
  const laneExchanges = useMemo(
    () => laneExchangeMap(lanes.data?.lanes),
    [lanes.data],
  );
  const [marketKey, setMarketKey] = useState("");
  const [timeframe, setTimeframe] = useState<ChartTimeframe>("15m");
  const [marketReadyKey, setMarketReadyKey] = useState<string | null>(null);
  const [showContext, setShowContext] = useState(true);
  const [chartError, setChartError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<ChartInstance | null>(null);
  const drawnIdsRef = useRef<string[]>([]);

  const selectedMarket =
    markets.find((market) => market.key === marketKey) ?? markets[0] ?? null;

  useEffect(() => {
    if (markets.length && !markets.some((market) => market.key === marketKey)) {
      setMarketKey(markets[0].key);
    }
  }, [marketKey, markets]);

  const candles = useQuery({
    queryKey: [
      "scanner-chart-candles",
      selectedMarket?.exchange,
      selectedMarket?.symbol,
      timeframe,
    ],
    queryFn: () => {
      if (!selectedMarket) throw new Error("no active chart market");
      return fetchChartCandles(
          selectedMarket.symbol,
          timeframe,
          500,
          selectedMarket.exchange,
        );
    },
    enabled: selectedMarket !== null,
    refetchInterval: 30_000,
  });
  const journal = useJournal(500, 0);
  // ML-plane mechanism context (swing levels, channel, FVG zones). Optional:
  // an older backend without the endpoint just means no context overlay.
  const context = useQuery({
    queryKey: [
      "scanner-chart-context",
      selectedMarket?.exchange,
      selectedMarket?.symbol,
      timeframe,
    ],
    queryFn: () => {
      if (!selectedMarket) throw new Error("no active chart market");
      return fetchMechanismContext(
        selectedMarket.symbol,
        timeframe,
        selectedMarket.exchange,
      );
    },
    enabled: selectedMarket !== null && showContext,
    refetchInterval: 60_000,
    retry: false,
  });

  const bars = useMemo<CandleBar[]>(
    () =>
      (candles.data?.candles ?? []).map((candle) => ({
        time: candle.time * 1000,
        open: candle.open,
        high: candle.high,
        low: candle.low,
        close: candle.close,
        volume: candle.volume,
      })),
    [candles.data],
  );
  const marketDataKey = useMemo(
    () =>
      bars.length && selectedMarket
        ? `${selectedMarket.key}:${timeframe}:${bars[0].time}:${bars[bars.length - 1].time}:${bars.length}`
        : null,
    [bars, selectedMarket, timeframe],
  );

  const plans = useMemo(
    () =>
      selectedMarket
        ? toPlans(
            journal.data?.scanner_events,
            selectedMarket,
            timeframe,
            laneExchanges,
          )
        : [],
    [journal.data, laneExchanges, selectedMarket, timeframe],
  );
  const eventMarkers = useMemo(
    () =>
      selectedMarket
        ? toEventMarkers(
            journal.data?.scanner_events,
            selectedMarket,
            timeframe,
            laneExchanges,
          )
        : [],
    [journal.data, laneExchanges, selectedMarket, timeframe],
  );

  // Market replacement is asynchronous in Vela. Drawings are added only after
  // ready()/setMarket() has finished, otherwise Vela clears them with old data.
  useEffect(() => {
    if (!containerRef.current || bars.length === 0 || !marketDataKey) return;
    let cancelled = false;
    setMarketReadyKey(null);

    const load = async () => {
      try {
        let chart = chartRef.current;
        if (chart) clearDrawings(chart, drawnIdsRef.current);
        drawnIdsRef.current = [];
        if (!chart) {
          chart = new Vela(containerRef.current as HTMLDivElement, {
            data: bars,
            timeframe,
            theme: "dark",
          });
          chartRef.current = chart;
          chart.drawings?.showToolbar?.(false);
          await chart.ready();
        } else {
          await chart.setMarket({ data: bars, timeframe });
        }
        if (cancelled || chartRef.current !== chart) return;
        setChartError(null);
        setMarketReadyKey(marketDataKey);
      } catch (error) {
        if (!cancelled) {
          setMarketReadyKey(null);
          setChartError(errorText(error));
        }
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [bars, marketDataKey, timeframe]);

  useEffect(
    () => () => {
      if (chartRef.current) {
        clearDrawings(chartRef.current, drawnIdsRef.current);
        chartRef.current.destroy?.();
      }
      chartRef.current = null;
      drawnIdsRef.current = [];
    },
    [],
  );

  // Journal overlays are bucketed onto the selected TF. Their cards retain
  // actual event time, so a 12:07 acceptance is drawn on the 12:00 15m bar but
  // remains labelled 12:07 instead of pretending the entry occurred at close.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || marketReadyKey !== marketDataKey || bars.length === 0) return;
    const drawings = chart.drawings;
    if (!drawings?.add) return;

    clearDrawings(chart, drawnIdsRef.current);
    drawnIdsRef.current = [];
    const lastTime = bars[bars.length - 1].time;
    const firstTime = bars[0].time;
    const barByTime = new Map(bars.map((bar) => [bar.time, bar]));
    let failures = 0;
    const keep = (drawing: { id: string } | null | undefined) => {
      if (drawing?.id) drawnIdsRef.current.push(drawing.id);
    };

    for (const marker of eventMarkers) {
      const bar = barByTime.get(marker.bar_ts_ms);
      if (!bar) continue;
      const entryLike = marker.kind !== "exit";
      const arrowUp = entryLike ? marker.side === "long" : marker.side === "short";
      const markerPrice = marker.event_price ?? (arrowUp ? bar.low : bar.high);
      try {
        const drawing = drawings.add(arrowUp ? "arrowmarkup" : "arrowmarkdown", {
          paneId: "price",
          anchors: [{ time: marker.bar_ts_ms, price: markerPrice }],
        });
        keep(drawing);
        if (drawing?.id) {
          drawings.update?.(drawing.id, {
            style: {
              lineColor: arrowUp ? COLORS.long : COLORS.short,
              fillColor: arrowUp ? COLORS.long : COLORS.short,
              lineWidth: 1,
              lineStyle: "solid",
            },
          });
          drawings.lock?.(drawing.id);
        }
      } catch {
        failures += 1;
      }
    }

    for (const plan of plans.slice(0, OVERLAY_PLANS)) {
      if (plan.bar_ts_ms < firstTime || plan.bar_ts_ms > lastTime) continue;
      try {
        const zone = drawings.add("box", {
          paneId: "price",
          anchors: [
            { time: plan.bar_ts_ms, price: plan.stop },
            { time: lastTime, price: plan.entry },
          ],
        });
        keep(zone);
        if (zone?.id) {
          drawings.update?.(zone.id, {
            style: {
              lineColor: COLORS.stop,
              lineWidth: 1,
              lineStyle: "dotted",
              fillColor: COLORS.stop,
              fillOpacity: 0.08,
            },
          });
          drawings.sendToBack?.(zone.id);
          drawings.lock?.(zone.id);
        }
        const lines: Array<[number | null, string]> = [
          [plan.entry, COLORS.entry],
          [plan.stop, COLORS.stop],
          [plan.target, COLORS.target],
        ];
        for (const [level, color] of lines) {
          if (typeof level !== "number") continue;
          const line = drawings.add("trendline", {
            paneId: "price",
            anchors: [
              { time: plan.bar_ts_ms, price: level },
              { time: lastTime, price: level },
            ],
          });
          keep(line);
          if (line?.id) {
            drawings.update?.(line.id, {
              style: { lineColor: color, lineWidth: 1, lineStyle: "dashed" },
            });
            drawings.lock?.(line.id);
          }
        }
      } catch {
        failures += 1;
      }
    }
    const ctx = showContext && context.data?.ready ? context.data : null;
    if (ctx) {
      const width = TF_MS[timeframe];
      const level = (
        value: number | null | undefined,
        color: string,
        style: "solid" | "dashed" | "dotted",
      ) => {
        if (typeof value !== "number" || !Number.isFinite(value)) return;
        try {
          const line = drawings.add("hline", {
            paneId: "price",
            anchors: [{ time: lastTime, price: value }],
          });
          keep(line);
          if (line?.id) {
            drawings.update?.(line.id, {
              style: { lineColor: color, lineWidth: 1, lineStyle: style },
            });
            drawings.lock?.(line.id);
          }
        } catch {
          failures += 1;
        }
      };
      level(ctx.swing_high, COLORS.swing, "solid");
      level(ctx.swing_low, COLORS.swing, "solid");
      level(ctx.donchian_high, COLORS.channel, "dotted");
      level(ctx.donchian_low, COLORS.channel, "dotted");
      level(
        ctx.supertrend_line,
        ctx.supertrend_dir === 1 ? COLORS.long : COLORS.short,
        "dashed",
      );
      for (const [zone, color] of [
        [ctx.bull_fvg, COLORS.long],
        [ctx.bear_fvg, COLORS.short],
      ] as const) {
        if (!zone) continue;
        const t0 = Math.max(firstTime, lastTime - zone.age_bars * width);
        try {
          const box = drawings.add("box", {
            paneId: "price",
            anchors: [
              { time: t0, price: zone.bottom },
              { time: lastTime, price: zone.top },
            ],
          });
          keep(box);
          if (box?.id) {
            drawings.update?.(box.id, {
              style: {
                lineColor: color,
                lineWidth: 1,
                lineStyle: "dotted",
                fillColor: color,
                fillOpacity: 0.06,
              },
            });
            drawings.sendToBack?.(box.id);
            drawings.lock?.(box.id);
          }
        } catch {
          failures += 1;
        }
      }
    }
    setChartError(failures ? `${failures} chart overlay(s) were rejected` : null);
  }, [bars, context.data, eventMarkers, marketDataKey, marketReadyKey, plans, showContext, timeframe]);

  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_320px]">
      <TerminalPanel
        title={`Scanner chart · ${selectedMarket ? baseAsset(selectedMarket.symbol) : "NO MARKET"} · ${timeframe}`}
        meta={`${selectedMarket?.exchange ?? "lane inventory unavailable"} · canonical lake · journal lifecycle overlays`}
      >
        <div className="mb-2 flex items-center gap-2 flex-wrap">
          {markets.map((market) => (
            <button
              key={market.key}
              onClick={() => setMarketKey(market.key)}
              className={`rounded border px-2 py-1 text-[11px] font-mono ${
                market.key === selectedMarket?.key
                  ? "border-brand text-brand"
                  : "border-line text-dim hover:text-txt"
              }`}
            >
              {market.label}
            </button>
          ))}
          <span className="mx-1 h-4 w-px bg-line" />
          {TIMEFRAMES.map((item) => (
            <button
              key={item}
              onClick={() => setTimeframe(item)}
              className={`rounded border px-2 py-1 text-[11px] font-mono ${
                item === timeframe
                  ? "border-brand text-brand"
                  : "border-line text-dim hover:text-txt"
              }`}
            >
              {item}
            </button>
          ))}
          <span className="mx-1 h-4 w-px bg-line" />
          <button
            onClick={() => setShowContext((value) => !value)}
            title="Mechanism context: swing levels, channel, supertrend, FVG zones — the ML plane's own view"
            className={`rounded border px-2 py-1 text-[11px] font-mono ${
              showContext
                ? "border-brand text-brand"
                : "border-line text-dim hover:text-txt"
            }`}
          >
            CTX
          </button>
          {showContext && context.data?.ready && (
            <span className="text-[10px] font-mono text-dim">
              vol&nbsp;
              {typeof context.data.atr_pctile === "number"
                ? `${Math.round(context.data.atr_pctile * 100)}%`
                : "—"}
              {" · st "}
              <span
                className={
                  context.data.supertrend_dir === 1 ? "text-long" : "text-short"
                }
              >
                {context.data.supertrend_dir === 1 ? "up" : "down"}
              </span>
            </span>
          )}
          {showContext && context.isError && (
            <span className="text-[10px] font-mono text-faint">ctx unavailable</span>
          )}
          <span className="ml-auto text-[10px] font-mono text-faint">
            {candles.data
              ? `${candles.data.count} bars · ${candles.data.source}`
              : candles.isError
                ? "candles unavailable"
                : "loading…"}
          </span>
        </div>
        <div className="relative">
          <div
            ref={containerRef}
            className="h-[520px] w-full rounded border border-line bg-black/20"
          />
          {chartError && (
            <div className="pointer-events-none absolute inset-x-4 top-4 rounded border border-short/40 bg-bg/90 px-3 py-2 text-[11px] font-mono text-short">
              Chart renderer: {chartError}
            </div>
          )}
          {!candles.isLoading && !candles.isError && bars.length === 0 && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-[11px] font-mono text-dim">
              {selectedMarket
                ? `No canonical ${timeframe} candles for ${selectedMarket.symbol} on ${selectedMarket.exchange}.`
                : "No active BTC/ETH lane is available for chart selection."}
            </div>
          )}
        </div>
      </TerminalPanel>

      <TerminalPanel title="Trade plans" meta="journal truth · actual event clocks">
        <div className="flex flex-col gap-2">
          {plans.length === 0 && (
            <div className="text-[11px] font-mono text-dim">
              No signal or accepted-entry plans for {selectedMarket?.symbol ?? "an active market"} @ {timeframe} in the journal tail.
            </div>
          )}
          {plans.slice(0, 8).map((plan) => {
            const risk = riskBps(plan);
            return (
              <div
                key={plan.key}
                className="rounded border border-line bg-bg/60 p-2 text-[11px] font-mono"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className={plan.side === "long" ? "text-long" : "text-short"}>
                    {plan.side.toUpperCase()} · {plan.strategy_id}
                  </span>
                  <TerminalBadge tone={plan.kind === "entry" ? "good" : "info"}>
                    {plan.kind === "entry" ? "accepted" : "signal"}
                  </TerminalBadge>
                </div>
                <div className="mt-1 grid grid-cols-3 gap-1 text-dim">
                  <span>E <span className="text-txt">{price(plan.entry)}</span></span>
                  <span>SL <span className="text-short">{price(plan.stop)}</span></span>
                  <span>TP <span className="text-long">{price(plan.target)}</span></span>
                </div>
                <div className="mt-1 flex items-center justify-between text-faint">
                  <span>{risk !== null ? `${risk.toFixed(1)} bps to stop` : "—"}</span>
                  <span>{new Date(plan.event_ts_ms).toISOString().slice(5, 16)}</span>
                </div>
                {plan.reason && (
                  <div className="mt-1 truncate text-faint" title={plan.reason}>
                    {plan.reason}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </TerminalPanel>
    </div>
  );
}
