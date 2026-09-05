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
import { TelescopeWorkspace } from "./TelescopeWorkspace";
import {
  canonicalChartSymbol,
  VnedgeDataProvider,
} from "../vela/VnedgeDataProvider";

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

export interface EventMarker {
  key: string;
  event_ts_ms: number;
  bar_ts_ms: number;
  side: string;
  kind: "evaluation" | "signal" | "entry" | "rejection" | "exit";
  event_price: number | null;
  reason: string;
  strategy_id: string;
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
  canonicalChartSymbol(raw);

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

function causalBarMs(event: ScannerAuditEvent, fallback: number, timeframe: ChartTimeframe) {
  const explicit = Date.parse(event.bar_ts || "");
  return Number.isFinite(explicit) ? explicit : bucketOpenMs(fallback, timeframe);
}

function evidenceKey(event: ScannerAuditEvent) {
  return event.decision_id || event.intent_key || event.permission_snapshot_id || event.ts;
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
      key: `${event.lane}:${evidenceKey(event)}:${event.kind}`,
      event_ts_ms: eventTs,
      bar_ts_ms: causalBarMs(event, eventTs, timeframe),
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

export function toEventMarkers(
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
    if (
      event.kind !== "signal" &&
      event.kind !== "evaluation" &&
      event.kind !== "entry" &&
      event.kind !== "rejection" &&
      event.kind !== "exit"
    ) {
      continue;
    }
    if (event.kind === "entry" && !event.approved) continue;
    const eventTs = eventTimeMs(event);
    if (!Number.isFinite(eventTs)) continue;
    const eventPrice = event.entry_price ?? event.decision_price ?? event.price;
    markers.push({
      key: `${event.lane}:${evidenceKey(event)}:${event.kind}`,
      event_ts_ms: eventTs,
      bar_ts_ms: causalBarMs(event, eventTs, timeframe),
      side: event.side,
      kind: event.kind,
      event_price: typeof eventPrice === "number" ? eventPrice : null,
      reason: event.reason,
      strategy_id: event.strategy_id,
    });
  }
  return markers.sort((left, right) => right.event_ts_ms - left.event_ts_ms);
}

export interface LifecycleSummary {
  evaluations: number;
  signals: number;
  accepted: number;
  rejected: number;
  exits: number;
}

export function lifecycleSummary(markers: EventMarker[]): LifecycleSummary {
  return markers.reduce<LifecycleSummary>(
    (summary, marker) => {
      if (marker.kind === "signal") summary.signals += 1;
      else if (marker.kind === "evaluation") summary.evaluations += 1;
      else if (marker.kind === "entry") summary.accepted += 1;
      else if (marker.kind === "rejection") summary.rejected += 1;
      else if (marker.kind === "exit") summary.exits += 1;
      return summary;
    },
    { evaluations: 0, signals: 0, accepted: 0, rejected: 0, exits: 0 },
  );
}

export interface SessionWindow {
  start: number;
  end: number;
  low: number;
  high: number;
}

/** UTC 12:00–16:00 research window, grouped into one band per day. */
export function activeSessionWindows(
  bars: CandleBar[],
  timeframe: ChartTimeframe,
): SessionWindow[] {
  const grouped = new Map<string, CandleBar[]>();
  for (const bar of bars) {
    const date = new Date(bar.time);
    const hour = date.getUTCHours();
    if (hour < 12 || hour >= 16) continue;
    const key = date.toISOString().slice(0, 10);
    const current = grouped.get(key) ?? [];
    current.push(bar);
    grouped.set(key, current);
  }
  return [...grouped.values()].map((items) => ({
    start: items[0].time,
    end: items[items.length - 1].time + TF_MS[timeframe],
    low: Math.min(...items.map((bar) => bar.low)),
    high: Math.max(...items.map((bar) => bar.high)),
  }));
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
  const [viewMode, setViewMode] = useState<"detail" | "telescope">("detail");
  const [showContext, setShowContext] = useState(true);
  const [showSession, setShowSession] = useState(true);
  const [annotations, setAnnotations] = useState(false);
  const [selectedEvidenceKey, setSelectedEvidenceKey] = useState<string>("");
  const [logScale, setLogScale] = useState(false);
  const [showProfile, setShowProfile] = useState(false);
  const vpvrRef = useRef<{ remove?: () => void } | null>(null);
  const [chartError, setChartError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<ChartInstance | null>(null);
  const providerRef = useRef<VnedgeDataProvider | null>(null);
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
  const chartIdentity = selectedMarket
    ? `${selectedMarket.key}:${timeframe}`
    : null;

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
  const lifecycle = useMemo(() => lifecycleSummary(eventMarkers), [eventMarkers]);
  const selectedEvidence =
    eventMarkers.find((marker) => marker.key === selectedEvidenceKey) ??
    eventMarkers[0] ??
    null;
  const selectedEvidenceBar = selectedEvidence
    ? bars.find((bar) => bar.time === selectedEvidence.bar_ts_ms) ?? null
    : null;

  // Vela owns its bar array through the canonical provider. We recreate only
  // on an operator market/TF switch; tail updates append through subscribe().
  // This avoids replacing a full bar array every poll (the source of the
  // renderer's "cannot update oldest data" ordering error).
  useEffect(() => {
    if (
      viewMode !== "detail" ||
      !containerRef.current ||
      !selectedMarket ||
      !chartIdentity
    ) return;
    let cancelled = false;
    setMarketReadyKey(null);
    const provider = new VnedgeDataProvider({
      exchange: selectedMarket.exchange,
      symbol: selectedMarket.symbol,
      label: selectedMarket.label,
    });
    providerRef.current = provider;
    const load = async () => {
      try {
        if (chartRef.current) {
          clearDrawings(chartRef.current, drawnIdsRef.current);
          chartRef.current.destroy?.();
        }
        drawnIdsRef.current = [];
        const chart = new Vela(containerRef.current as HTMLDivElement, {
          symbol: `vnedge:${canonicalChartSymbol(selectedMarket.symbol)}`,
          timeframe,
          bars: 500,
          live: true,
          theme: "dark",
        });
        chartRef.current = chart;
        chart.drawings?.showToolbar?.(false);
        await chart.data.registerProvider("vnedge", provider);
        try {
          const saved = localStorage.getItem("vnedge.chart.config");
          if (saved) chart.renderer?.applyConfig?.(JSON.parse(saved));
        } catch {
          /* cosmetics only — never block the chart on bad persisted config */
        }
        await chart.ready();
        if (cancelled || chartRef.current !== chart) return;
        setChartError(null);
        setMarketReadyKey(chartIdentity);
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
      if (chartRef.current) {
        clearDrawings(chartRef.current, drawnIdsRef.current);
        chartRef.current.destroy?.();
      }
      chartRef.current = null;
      provider.destroy();
      if (providerRef.current === provider) providerRef.current = null;
      drawnIdsRef.current = [];
    };
  }, [
    chartIdentity,
    selectedMarket?.exchange,
    selectedMarket?.label,
    selectedMarket?.symbol,
    timeframe,
    viewMode,
  ]);

  useEffect(
    () => () => {
      if (chartRef.current) {
        clearDrawings(chartRef.current, drawnIdsRef.current);
        chartRef.current.destroy?.();
      }
      chartRef.current = null;
      providerRef.current?.destroy();
      providerRef.current = null;
      drawnIdsRef.current = [];
      vpvrRef.current = null;
    },
    [],
  );

  // Renderer features: log scale + visible-range volume profile (native).
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || marketReadyKey !== chartIdentity) return;
    try {
      chart.renderer?.set?.({ logScale });
    } catch {
      /* renderer without the feature: ignore */
    }
    try {
      if (showProfile && !vpvrRef.current) {
        vpvrRef.current = chart.addNativeIndicator?.("vpvr") ?? null;
      } else if (!showProfile && vpvrRef.current) {
        vpvrRef.current.remove?.();
        vpvrRef.current = null;
      }
    } catch {
      vpvrRef.current = null;
    }
  }, [chartIdentity, logScale, marketReadyKey, showProfile]);

  useEffect(() => {
    chartRef.current?.drawings?.showToolbar?.(annotations);
  }, [annotations]);

  // Journal overlays are bucketed onto the selected TF. Their cards retain
  // actual event time, so a 12:07 acceptance is drawn on the 12:00 15m bar but
  // remains labelled 12:07 instead of pretending the entry occurred at close.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || marketReadyKey !== chartIdentity || bars.length === 0) return;
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
      if (marker.kind === "evaluation") continue;
      const bar = barByTime.get(marker.bar_ts_ms);
      if (!bar) continue;
      if (marker.kind === "rejection") {
        const isQuality = /gap|lag|timeout|stale|candle|data/i.test(marker.reason);
        if (!isQuality) continue;
        try {
          const band = drawings.add("box", {
            paneId: "price",
            anchors: [
              { time: marker.bar_ts_ms, price: bar.low },
              { time: marker.bar_ts_ms + TF_MS[timeframe], price: bar.high },
            ],
          });
          keep(band);
          if (band?.id) {
            drawings.update?.(band.id, {
              style: {
                lineColor: COLORS.short,
                lineWidth: 1,
                lineStyle: "dotted",
                fillColor: COLORS.short,
                fillOpacity: 0.12,
              },
            });
            drawings.sendToBack?.(band.id);
            drawings.lock?.(band.id);
          }
        } catch {
          failures += 1;
        }
        continue;
      }
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

    if (showSession) {
      for (const session of activeSessionWindows(bars, timeframe)) {
        try {
          const band = drawings.add("box", {
            paneId: "price",
            anchors: [
              { time: session.start, price: session.low },
              { time: session.end, price: session.high },
            ],
          });
          keep(band);
          if (band?.id) {
            drawings.update?.(band.id, {
              style: {
                lineColor: COLORS.entry,
                lineWidth: 1,
                lineStyle: "dotted",
                fillColor: COLORS.entry,
                fillOpacity: 0.035,
              },
            });
            drawings.sendToBack?.(band.id);
            drawings.lock?.(band.id);
          }
        } catch {
          failures += 1;
        }
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
  }, [bars, chartIdentity, context.data, eventMarkers, marketReadyKey, plans, showContext, showSession, timeframe]);

  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_320px]">
      <TerminalPanel
        title={`${viewMode === "telescope" ? "Market telescope" : "Scanner chart"} · ${selectedMarket ? baseAsset(selectedMarket.symbol) : "NO MARKET"}${viewMode === "detail" ? ` · ${timeframe}` : ""}`}
        meta={`${selectedMarket?.exchange ?? "lane inventory unavailable"} · VNEDGE provider · display only`}
      >
        <div className="mb-2 flex items-center gap-2 flex-wrap">
          <button
            onClick={() => setViewMode("detail")}
            className={`rounded border px-2 py-1 text-[11px] font-mono ${
              viewMode === "detail"
                ? "border-brand text-brand"
                : "border-line text-dim hover:text-txt"
            }`}
          >
            DETAIL
          </button>
          <button
            onClick={() => setViewMode("telescope")}
            className={`rounded border px-2 py-1 text-[11px] font-mono ${
              viewMode === "telescope"
                ? "border-brand text-brand"
                : "border-line text-dim hover:text-txt"
            }`}
          >
            4-TF
          </button>
          <span className="mx-1 h-4 w-px bg-line" />
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
          {viewMode === "detail" && <span className="mx-1 h-4 w-px bg-line" />}
          {viewMode === "detail" && TIMEFRAMES.map((item) => (
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
          {viewMode === "detail" && <span className="mx-1 h-4 w-px bg-line" />}
          {viewMode === "detail" && <button
            onClick={() => setShowContext((value) => !value)}
            title="Mechanism context: swing levels, channel, supertrend, FVG zones — the ML plane's own view"
            className={`rounded border px-2 py-1 text-[11px] font-mono ${
              showContext
                ? "border-brand text-brand"
                : "border-line text-dim hover:text-txt"
            }`}
          >
            CTX
          </button>}
          {viewMode === "detail" && showContext && context.data?.ready && (
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
          {viewMode === "detail" && showContext && context.isError && (
            <span className="text-[10px] font-mono text-faint">ctx unavailable</span>
          )}
          {viewMode === "detail" && <button
            onClick={() => setShowSession((value) => !value)}
            title="Shade the pre-registered 12:00–16:00 UTC research window"
            className={`rounded border px-2 py-1 text-[11px] font-mono ${
              showSession ? "border-brand text-brand" : "border-line text-dim hover:text-txt"
            }`}
          >
            SESSION
          </button>}
          {viewMode === "detail" && <button
            onClick={() => setAnnotations((value) => !value)}
            title="Operator annotations; drawings are presentation-only"
            className={`rounded border px-2 py-1 text-[11px] font-mono ${
              annotations ? "border-brand text-brand" : "border-line text-dim hover:text-txt"
            }`}
          >
            DRAW
          </button>}
          {viewMode === "detail" && <span className="mx-1 h-4 w-px bg-line" />}
          {viewMode === "detail" && <button
            onClick={() =>
              setLogScale((value) => {
                const next = !value;
                try {
                  const config = chartRef.current?.renderer?.getConfig?.();
                  if (config) localStorage.setItem("vnedge.chart.config", JSON.stringify(config));
                } catch {
                  /* persistence is best-effort */
                }
                return next;
              })
            }
            title="Logarithmic price scale"
            className={`rounded border px-2 py-1 text-[11px] font-mono ${
              logScale ? "border-brand text-brand" : "border-line text-dim hover:text-txt"
            }`}
          >
            LOG
          </button>}
          {viewMode === "detail" && <button
            onClick={() => setShowProfile((value) => !value)}
            title="Visible-range volume profile (native indicator)"
            className={`rounded border px-2 py-1 text-[11px] font-mono ${
              showProfile ? "border-brand text-brand" : "border-line text-dim hover:text-txt"
            }`}
          >
            VPVR
          </button>}
          {viewMode === "detail" && <button
            onClick={() => {
              try {
                const url = chartRef.current?.renderer?.screenshot?.();
                if (!url) return;
                const link = document.createElement("a");
                link.href = url;
                link.download = `vnedge-${selectedMarket ? baseAsset(selectedMarket.symbol) : "chart"}-${timeframe}-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "")}.png`;
                link.click();
              } catch {
                /* screenshot unsupported on this renderer */
              }
            }}
            title="Export chart as PNG (candles + overlays; journal evidence stays in the journal)"
            className="rounded border border-line px-2 py-1 text-[11px] font-mono text-dim hover:text-txt"
          >
            PNG
          </button>}
          <span className="ml-auto text-[10px] font-mono text-faint">
            {viewMode === "telescope"
              ? "4h → 1h → 15m → 5m"
              : candles.data
              ? `${candles.data.count} bars · ${candles.data.source}`
              : candles.isError
                ? "candles unavailable"
                : "loading…"}
          </span>
        </div>
        {viewMode === "telescope" ? (
          <TelescopeWorkspace market={selectedMarket} />
        ) : <div className="relative">
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
        </div>}
      </TerminalPanel>

      <TerminalPanel title="Evidence inspector" meta="journal truth · actual event clocks">
        <div className="flex flex-col gap-2">
          <div className="grid grid-cols-5 gap-1 rounded border border-line bg-bg/60 p-2 text-center font-mono">
            {[
              ["eval", lifecycle.evaluations],
              ["signal", lifecycle.signals],
              ["accept", lifecycle.accepted],
              ["reject", lifecycle.rejected],
              ["exit", lifecycle.exits],
            ].map(([label, value]) => (
              <div key={String(label)}>
                <div className="text-[9px] uppercase text-faint">{label}</div>
                <div className="text-[12px] text-txt">{value}</div>
              </div>
            ))}
          </div>
          <div className="text-[10px] font-mono text-faint">
            Journal-tail lifecycle · absence is shown, never inferred as healthy.
          </div>
          {selectedEvidence && (
            <div className="rounded border border-brand/40 bg-brand/5 p-2 text-[11px] font-mono">
              <div className="flex items-center justify-between gap-2">
                <span className="text-brand">SELECTED EVIDENCE</span>
                <TerminalBadge tone={selectedEvidence.kind === "entry" ? "good" : selectedEvidence.kind === "rejection" ? "bad" : "info"}>
                  {selectedEvidence.kind}
                </TerminalBadge>
              </div>
              <div className="mt-2 text-txt">{selectedEvidence.strategy_id || "unattributed scanner"}</div>
              <div className="mt-1 break-words text-faint">{selectedEvidence.reason || "no reason reported"}</div>
              <div className="mt-2 grid grid-cols-2 gap-1 text-dim">
                <span>event</span><span className="text-right text-txt">{new Date(selectedEvidence.event_ts_ms).toISOString().slice(5, 19).replace("T", " ")}</span>
                <span>causal bar</span><span className="text-right text-txt">{new Date(selectedEvidence.bar_ts_ms).toISOString().slice(5, 16).replace("T", " ")}</span>
                <span>OHLC</span><span className="text-right text-txt">{selectedEvidenceBar ? `${price(selectedEvidenceBar.open)} / ${price(selectedEvidenceBar.high)} / ${price(selectedEvidenceBar.low)} / ${price(selectedEvidenceBar.close)}` : "outside chart window"}</span>
                <span>volume</span><span className="text-right text-txt">{selectedEvidenceBar ? price(selectedEvidenceBar.volume) : "—"}</span>
              </div>
            </div>
          )}
          {eventMarkers.slice(0, 10).map((marker) => (
            <button
              key={marker.key}
              onClick={() => setSelectedEvidenceKey(marker.key)}
              className={`rounded border p-2 text-left text-[10px] font-mono ${
                selectedEvidence?.key === marker.key
                  ? "border-brand bg-brand/5"
                  : "border-line bg-bg/40 hover:border-brand/50"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-txt">{marker.kind.toUpperCase()} · {marker.strategy_id || "scanner"}</span>
                <span className="text-faint">{new Date(marker.event_ts_ms).toISOString().slice(11, 19)}</span>
              </div>
              <div className="mt-1 truncate text-faint" title={marker.reason}>{marker.reason || "no reason reported"}</div>
            </button>
          ))}
          <div className="my-1 border-t border-line" />
          <div className="text-[10px] font-mono text-dim">TRADE PLANS</div>
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
