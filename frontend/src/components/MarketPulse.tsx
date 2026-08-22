import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PulseHour } from "../api";
import { useHourAnalysis, useLanes, usePulse, useRiskSnapshot } from "../queries";
import { ScannerWorkspace } from "./ScannerWorkspace";
import { TerminalBadge, TerminalPanel } from "./Terminal";

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];

const fmt = (value: number | null | undefined, digits = 1) =>
  typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";

const signed = (value: number | null | undefined, digits = 1) =>
  typeof value === "number" && Number.isFinite(value)
    ? `${value > 0 ? "+" : ""}${value.toFixed(digits)}`
    : "—";

const priceText = (value: number | null | undefined) => {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const maximumFractionDigits = value >= 1_000 ? 2 : value >= 1 ? 4 : 8;
  return new Intl.NumberFormat("en-US", { maximumFractionDigits }).format(value);
};

const ageSecMs = (milliseconds: number) => {
  const seconds = Math.max(0, milliseconds / 1_000);
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5_400) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3_600).toFixed(1)}h`;
};

const utcHour = (iso: string) =>
  new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    hour12: false,
    timeZone: "UTC",
  }).format(new Date(iso));

const fullUtcHour = (iso: string) =>
  new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  }).format(new Date(iso));

const toUnixHour = (value: string): UTCTimestamp | null => {
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) return null;
  return Math.floor(milliseconds / 3_600_000) * 3_600 as UTCTimestamp;
};

const utcTimeLabel = (time: Time) => {
  if (typeof time !== "number") return String(time);
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  }).format(new Date(time * 1_000));
};

type CandlePoint = CandlestickData<UTCTimestamp> | WhitespaceData<UTCTimestamp>;
type VwapPoint = LineData<UTCTimestamp> | WhitespaceData<UTCTimestamp>;

function chartPoints(hours: PulseHour[]): {
  candles: CandlePoint[];
  vwap: VwapPoint[];
  avwapLow: VwapPoint[];
  avwapHigh: VwapPoint[];
  signature: string;
} {
  const byTime = new Map<number, PulseHour>();
  for (const hour of hours) {
    const time = toUnixHour(hour.open_time);
    if (
      time !== null
      && [hour.open, hour.high, hour.low, hour.close].every(Number.isFinite)
    ) {
      byTime.set(time, hour);
    }
  }
  const times = [...byTime.keys()].sort((a, b) => a - b);
  const timeline: number[] = [];
  times.forEach((time, index) => {
    const previous = times[index - 1];
    if (previous !== undefined && time - previous > 3_600) {
      // Whitespace preserves unproven hours without inventing zero-volume OHLC.
      const missingHours = Math.min(Math.floor((time - previous) / 3_600) - 1, 168);
      for (let offset = 1; offset <= missingHours; offset += 1) {
        timeline.push(previous + offset * 3_600);
      }
    }
    timeline.push(time);
  });

  const candles: CandlePoint[] = [];
  const vwap: VwapPoint[] = [];
  const avwapLow: VwapPoint[] = [];
  const avwapHigh: VwapPoint[] = [];
  for (const rawTime of timeline) {
    const time = rawTime as UTCTimestamp;
    const hour = byTime.get(rawTime);
    if (!hour) {
      candles.push({ time });
      vwap.push({ time });
      avwapLow.push({ time });
      avwapHigh.push({ time });
      continue;
    }
    const degraded = hour.data_quality !== "ok";
    candles.push({
      time,
      open: hour.open,
      high: hour.high,
      low: hour.low,
      close: hour.close,
      ...(degraded
        ? { color: "#6E7681", borderColor: "#6E7681", wickColor: "#6E7681" }
        : {}),
    });
    vwap.push(
      hour.session_vwap == null || !Number.isFinite(hour.session_vwap)
        ? { time }
        : { time, value: hour.session_vwap },
    );
    avwapLow.push(
      hour.avwap_low == null || !Number.isFinite(hour.avwap_low)
        ? { time }
        : { time, value: hour.avwap_low },
    );
    avwapHigh.push(
      hour.avwap_high == null || !Number.isFinite(hour.avwap_high)
        ? { time }
        : { time, value: hour.avwap_high },
    );
  }
  return {
    candles,
    vwap,
    avwapLow,
    avwapHigh,
    signature: hours
      .map((hour) => [
        hour.symbol,
        hour.open_time,
        hour.open,
        hour.high,
        hour.low,
        hour.close,
        hour.session_vwap,
        hour.avwap_low,
        hour.avwap_high,
        hour.avwap_low_anchor_utc,
        hour.avwap_high_anchor_utc,
        hour.data_quality,
      ].join(":"))
      .join("|"),
  };
}

function formingPoint(
  forming: Record<string, unknown> | null | undefined,
  asOf: string | undefined,
): CandlestickData<UTCTimestamp> | null {
  if (!forming) return null;
  const open = Number(forming.open);
  const high = Number(forming.high);
  const low = Number(forming.low);
  const close = Number(forming.close);
  const rawTime = typeof forming.open_time === "string" ? forming.open_time : asOf;
  const time = rawTime ? toUnixHour(rawTime) : null;
  if (time === null || ![open, high, low, close].every(Number.isFinite)) return null;
  return {
    time,
    open,
    high,
    low,
    close,
    color: "#58A6FF",
    borderColor: "#58A6FF",
    wickColor: "#58A6FF",
  };
}

function CandleChart({
  hours,
  forming,
  asOf,
  selected,
  avwap,
  avwapLabel,
  priorDayPoc,
  priorDayVah,
  priorDayVal,
}: {
  hours: PulseHour[];
  forming: Record<string, unknown> | null | undefined;
  asOf: string | undefined;
  selected: string | null;
  avwap: number | null | undefined;
  avwapLabel: string | null | undefined;
  priorDayPoc: number | null | undefined;
  priorDayVah: number | null | undefined;
  priorDayVal: number | null | undefined;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vwapRef = useRef<ISeriesApi<"Line"> | null>(null);
  const lowAvwapRef = useRef<ISeriesApi<"Line"> | null>(null);
  const highAvwapRef = useRef<ISeriesApi<"Line"> | null>(null);
  const avwapRef = useRef<IPriceLine | null>(null);
  const pocRef = useRef<IPriceLine | null>(null);
  const vahRef = useRef<IPriceLine | null>(null);
  const valRef = useRef<IPriceLine | null>(null);
  const markerRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const historySignatureRef = useRef("");
  const formingTimeRef = useRef<UTCTimestamp | null>(null);
  const fittedRef = useRef(false);
  const [rendererError, setRendererError] = useState<string | null>(null);
  const [crosshair, setCrosshair] = useState<{
    time: Time;
    open: number;
    high: number;
    low: number;
    close: number;
  } | null>(null);
  const points = useMemo(() => chartPoints(hours), [hours]);
  const livePoint = useMemo(() => formingPoint(forming, asOf), [forming, asOf]);
  const latestHistoryTime = points.candles.length
    ? points.candles[points.candles.length - 1].time
    : null;
  const formingWithheld = (
    livePoint !== null
    && latestHistoryTime !== null
    && livePoint.time <= latestHistoryTime
  );
  // lightweight-charts requires update.time >= the last series time. The
  // runtime forming clock can briefly trail canonical storage at an hour
  // boundary, so withhold that point instead of letting the chart throw.
  const chartLivePoint = formingWithheld ? null : livePoint;
  const hasData = hours.length > 0 || chartLivePoint !== null;
  const hasDegradedHours = hours.some((hour) => hour.data_quality !== "ok");
  const hasDualAvwap = hours.some((hour) => hour.avwap_low != null || hour.avwap_high != null);

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    container.dataset.chartState = "effect";
    try {
      const chart = createChart(container, {
        width: container.clientWidth,
        height: 360,
        layout: {
          background: { type: ColorType.Solid, color: "#111318" },
          textColor: "#8B949E",
          fontFamily: "JetBrains Mono, IBM Plex Mono, ui-monospace, monospace",
          fontSize: 11,
          attributionLogo: true,
        },
        grid: {
          vertLines: { color: "#21262D", style: LineStyle.Dotted },
          horzLines: { color: "#21262D", style: LineStyle.Dotted },
        },
        crosshair: {
          mode: CrosshairMode.Normal,
          vertLine: { color: "#6E7681", labelBackgroundColor: "#30363D" },
          horzLine: { color: "#6E7681", labelBackgroundColor: "#30363D" },
        },
        rightPriceScale: {
          borderColor: "#30363D",
          scaleMargins: { top: 0.12, bottom: 0.12 },
        },
        timeScale: {
          borderColor: "#30363D",
          timeVisible: true,
          secondsVisible: false,
          rightOffset: 2,
          barSpacing: 10,
          minBarSpacing: 4,
          tickMarkFormatter: (time: Time) => utcTimeLabel(time),
        },
        localization: {
          locale: "en-GB",
          timeFormatter: (time: Time) => `${utcTimeLabel(time)} UTC`,
        },
      });
      container.dataset.chartState = "chart";
      const candles = chart.addSeries(CandlestickSeries, {
        upColor: "#3FB950",
        downColor: "#F85149",
        borderVisible: false,
        wickUpColor: "#3FB950",
        wickDownColor: "#F85149",
        priceLineVisible: false,
        lastValueVisible: true,
      });
      const vwap = chart.addSeries(LineSeries, {
        color: "#D29922",
        lineWidth: 2,
        lineStyle: LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: true,
        title: "session VWAP",
      });
      const lowAvwap = chart.addSeries(LineSeries, {
        color: "#58A6FF",
        lineWidth: 2,
        lineStyle: LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: true,
        title: "AVWAP L",
      });
      const highAvwap = chart.addSeries(LineSeries, {
        color: "#BC8CFF",
        lineWidth: 2,
        lineStyle: LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: true,
        title: "AVWAP H",
      });
      container.dataset.chartState = "ready";
      chartRef.current = chart;
      candleRef.current = candles;
      vwapRef.current = vwap;
      lowAvwapRef.current = lowAvwap;
      highAvwapRef.current = highAvwap;
      markerRef.current = createSeriesMarkers(candles, []);
      chart.subscribeCrosshairMove((param) => {
        const value = param.seriesData.get(candles);
        if (
          param.time != null
          && value
          && "open" in value
          && "high" in value
          && "low" in value
          && "close" in value
        ) {
          setCrosshair({
            time: param.time,
            open: Number(value.open),
            high: Number(value.high),
            low: Number(value.low),
            close: Number(value.close),
          });
        } else {
          setCrosshair(null);
        }
      });

      const observer = new ResizeObserver(([entry]) => {
        chart.applyOptions({ width: Math.floor(entry.contentRect.width) });
      });
      observer.observe(container);
      return () => {
        observer.disconnect();
        chart.remove();
        chartRef.current = null;
        candleRef.current = null;
        vwapRef.current = null;
        lowAvwapRef.current = null;
        highAvwapRef.current = null;
        avwapRef.current = null;
        pocRef.current = null;
        vahRef.current = null;
        valRef.current = null;
        markerRef.current = null;
        historySignatureRef.current = "";
        formingTimeRef.current = null;
        fittedRef.current = false;
      };
    } catch (error) {
      container.dataset.chartState = "error";
      setRendererError(error instanceof Error ? error.message : "unknown chart error");
      return undefined;
    }
  }, []); // history markers are re-created when the chart mounts for a symbol

  useEffect(() => {
    const chart = chartRef.current;
    const candles = candleRef.current;
    const vwap = vwapRef.current;
    const lowAvwap = lowAvwapRef.current;
    const highAvwap = highAvwapRef.current;
    if (!chart || !candles || !vwap || !lowAvwap || !highAvwap) return;

    const historyChanged = historySignatureRef.current !== points.signature;
    try {
      const formingRolled = (
        formingTimeRef.current !== null
        && chartLivePoint !== null
        && formingTimeRef.current !== chartLivePoint.time
      );
      const formingCleared = formingTimeRef.current !== null && chartLivePoint === null;
      if (historyChanged || formingRolled || formingCleared) {
        candles.setData(points.candles);
        vwap.setData(points.vwap);
        lowAvwap.setData(points.avwapLow);
        highAvwap.setData(points.avwapHigh);
        historySignatureRef.current = points.signature;
        if (!fittedRef.current && points.candles.length > 0) {
          chart.timeScale().fitContent();
          fittedRef.current = true;
        }
      }
      if (chartLivePoint) candles.update(chartLivePoint);
      formingTimeRef.current = chartLivePoint?.time ?? null;
      setRendererError(null);
    } catch (error) {
      formingTimeRef.current = null;
      setRendererError(error instanceof Error ? error.message : "unknown chart update error");
    }
  }, [chartLivePoint, points]);

  useEffect(() => {
    markerRef.current?.setMarkers(
      hours.flatMap((hour) => {
        const time = toUnixHour(hour.open_time);
        if (time === null || hour.data_quality === "ok") return [];
        return [{
          time,
          position: "aboveBar" as const,
          color: "#F85149",
          shape: "circle" as const,
          text: "GAP",
        }];
      }),
    );
  }, [hours]);

  useEffect(() => {
    const chart = chartRef.current;
    const time = selected ? toUnixHour(selected) : null;
    if (!chart || time === null) return;
    chart.timeScale().setVisibleRange({
      from: (time - 23 * 3_600) as UTCTimestamp,
      to: (time + 3_600) as UTCTimestamp,
    });
  }, [selected]);

  useEffect(() => {
    const candles = candleRef.current;
    if (!candles) return;
    if (avwapRef.current) {
      candles.removePriceLine(avwapRef.current);
      avwapRef.current = null;
    }
    if (typeof avwap === "number" && Number.isFinite(avwap)) {
      avwapRef.current = candles.createPriceLine({
        price: avwap,
        color: "#58A6FF",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: avwapLabel || "AVWAP",
      });
    }
  }, [avwap, avwapLabel]);

  useEffect(() => {
    const candles = candleRef.current;
    if (!candles) return;
    if (pocRef.current) {
      candles.removePriceLine(pocRef.current);
      pocRef.current = null;
    }
    if (vahRef.current) {
      candles.removePriceLine(vahRef.current);
      vahRef.current = null;
    }
    if (valRef.current) {
      candles.removePriceLine(valRef.current);
      valRef.current = null;
    }
    if (typeof priorDayPoc === "number" && Number.isFinite(priorDayPoc)) {
      pocRef.current = candles.createPriceLine({
        price: priorDayPoc,
        color: "#F0883E",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "PRIOR-DAY POC",
      });
    }
    if (typeof priorDayVah === "number" && Number.isFinite(priorDayVah)) {
      vahRef.current = candles.createPriceLine({
        price: priorDayVah,
        color: "#8B949E",
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: true,
        title: "VAH",
      });
    }
    if (typeof priorDayVal === "number" && Number.isFinite(priorDayVal)) {
      valRef.current = candles.createPriceLine({
        price: priorDayVal,
        color: "#8B949E",
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: true,
        title: "VAL",
      });
    }
  }, [priorDayPoc, priorDayVah, priorDayVal]);

  return (
    <div className="relative overflow-hidden rounded-md bg-inset">
      <div
        ref={containerRef}
        className="h-[360px] w-full"
        role="img"
        aria-label={`${hours.length} closed one-hour candlesticks with session VWAP${chartLivePoint ? " and one forming hour" : ""}. Times are UTC.`}
      />
      {rendererError && (
        <div className="absolute inset-0 z-20 grid place-items-center bg-inset px-6 text-center font-mono text-xs text-short">
          Chart renderer unavailable: {rendererError}
        </div>
      )}
      {!hasData && !rendererError && (
        <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center bg-inset font-mono text-sm text-dim">
          No 1h candles yet.
        </div>
      )}
      <div className="pointer-events-none absolute left-3 top-3 z-10 flex flex-wrap items-center gap-3 rounded-md border border-line bg-bg/90 px-2.5 py-1.5 font-mono text-[10px] shadow-lg">
        <span className="flex items-center gap-1.5 text-warn"><span className="h-0.5 w-4 bg-warn" />SESSION VWAP</span>
        {hasDualAvwap && <span className="flex items-center gap-1.5 text-info"><span className="h-0.5 w-4 bg-info" />AVWAP L</span>}
        {hasDualAvwap && <span className="flex items-center gap-1.5 text-[#BC8CFF]"><span className="h-0.5 w-4 bg-[#BC8CFF]" />AVWAP H</span>}
        {typeof priorDayPoc === "number" && Number.isFinite(priorDayPoc) && <span className="flex items-center gap-1.5 text-[#F0883E]"><span className="h-0.5 w-4 bg-[#F0883E]" />PRIOR-DAY POC</span>}
        {typeof priorDayVah === "number" && typeof priorDayVal === "number" && <span className="flex items-center gap-1.5 text-dim"><span className="h-px w-4 border-t border-dotted border-dim" />VAH / VAL</span>}
        {chartLivePoint && <span className="flex items-center gap-1.5 text-info"><span className="h-2 w-2 bg-info" />FORMING</span>}
        {formingWithheld && <span className="text-warn">STALE FORMING POINT WITHHELD</span>}
        {hasDegradedHours && <span className="text-faint">MUTED = GAP/DEGRADED</span>}
      </div>
      {crosshair && (
        <div className="pointer-events-none absolute right-3 top-3 z-10 rounded-md border border-line bg-bg/95 px-3 py-2 font-mono text-[10px] shadow-lg">
          <div className="mb-1 text-brand">{utcTimeLabel(crosshair.time)} UTC</div>
          <div className="grid grid-cols-4 gap-2 text-dim"><span>O {fmt(crosshair.open, 2)}</span><span>H {fmt(crosshair.high, 2)}</span><span>L {fmt(crosshair.low, 2)}</span><span>C {fmt(crosshair.close, 2)}</span></div>
          <div className="mt-1 text-faint">range {fmt((crosshair.high - crosshair.low) / crosshair.open * 10_000)} bps · body {fmt(Math.abs(crosshair.close - crosshair.open) / crosshair.open * 10_000)} bps</div>
        </div>
      )}
      {selected && (
        <div className="pointer-events-none absolute bottom-3 left-3 z-10 rounded-md border border-line bg-bg/90 px-2 py-1 font-mono text-[10px] text-dim">
          selected {fullUtcHour(selected)} UTC
        </div>
      )}
    </div>
  );
}

function Metric({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="rounded-lg border border-line bg-inset/80 px-3 py-3">
      <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-faint">{label}</div>
      <div className="mt-1 font-mono text-[18px] tabular-nums text-txt">{value}</div>
      {note && <div className="mt-1 text-[10px] text-dim">{note}</div>}
    </div>
  );
}

export function MarketPulse() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [selected, setSelected] = useState<string | null>(null);
  const [alertFilter, setAlertFilter] = useState<"active" | "all" | "recovered">("active");
  const btcPulse = usePulse("BTCUSDT");
  const ethPulse = usePulse("ETHUSDT");
  const solPulse = usePulse("SOLUSDT");
  const risk = useRiskSnapshot();
  const lanes = useLanes();
  const pulse = symbol === "ETHUSDT" ? ethPulse : symbol === "SOLUSDT" ? solPulse : btcPulse;
  const marketQueries = [btcPulse, ethPulse, solPulse];
  const hours = pulse.data?.hours ?? [];
  const latest = hours[hours.length - 1];

  useEffect(() => {
    if (!selected || !hours.some((hour) => hour.open_time === selected)) {
      setSelected(latest?.open_time ?? null);
    }
  }, [hours, latest?.open_time, selected]);

  const selectedHour = useMemo(
    () => hours.find((hour) => hour.open_time === selected) ?? latest,
    [hours, latest, selected],
  );
  const analysis = useHourAnalysis(symbol, selectedHour?.open_time ?? null);
  const quality = pulse.data?.data_quality ?? "unknown";
  const forming = pulse.data?.forming;
  const book = pulse.data?.book;
  const formingRange = forming?.range_bps as number | undefined;
  const formingRangeVsMedian = forming?.range_vs_median_24h as number | undefined;
  const formingVolumeRank = forming?.volume_rank_24h as number | undefined;
  const formingVolumeVsMedian = forming?.volume_vs_median_24h as number | undefined;
  const formingActive = forming?.status === "forming";
  const dualAvwapNote = forming?.dual_avwap_reason
    ?? pulse.data?.indicators.avwap_unavailable_reason
    ?? (
      pulse.data?.indicators.avwap_low_anchor_utc
      && pulse.data?.indicators.avwap_high_anchor_utc
        ? `L ${fullUtcHour(pulse.data.indicators.avwap_low_anchor_utc)} · H ${fullUtcHour(pulse.data.indicators.avwap_high_anchor_utc)} UTC`
        : undefined
    );
  const priorDayProfile = pulse.data?.volume_profile.prior_day;
  const profileReason = priorDayProfile?.reason?.replace(/_/g, " ");
  const qualityNote = pulse.data?.last_gap
    ? pulse.data.last_gap.recovered
      ? `last gap recovered · ${fullUtcHour(pulse.data.last_gap.start)} UTC`
      : `active ${pulse.data.last_gap.kind.replace(/_/g, " ")} · ${fullUtcHour(pulse.data.last_gap.start)} UTC`
    : "no recorded gap in window";
  const events = [
    ...(pulse.data?.alerts ?? []),
    ...((risk.data?.gateway.last_reject_reasons ?? []).map((item) => ({
      kind: "cost_or_risk_reject",
      at: pulse.data?.as_of ?? new Date().toISOString(),
      severity: "warning" as const,
      message: `${item.reason} · ${item.count} observed in current snapshot`,
      recovered: true,
    }))),
    ...(risk.data?.kill.active ? [{
      kind: "kill",
      at: pulse.data?.as_of ?? new Date().toISOString(),
      severity: "critical" as const,
      message: "Kill switch is active and latched.",
      recovered: false,
    }] : []),
    ...(risk.data?.daily_halt.active ? [{
      kind: "daily_halt",
      at: pulse.data?.as_of ?? new Date().toISOString(),
      severity: "critical" as const,
      message: "Daily loss halt is active.",
      recovered: false,
    }] : []),
  ];
  const visibleEvents = events.filter((event) => alertFilter === "all" || (alertFilter === "recovered" ? event.recovered : !event.recovered));
  const activeEventCount = events.filter((event) => !event.recovered).length;

  return (
    <div className="flex flex-col gap-4">
      <section className="rounded-xl border border-line bg-panel/80 px-4 py-4 md:px-5 flex items-center justify-between gap-4 flex-wrap">
        <div>
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-xl font-semibold tracking-tight">Market Pulse</h1>
            <TerminalBadge tone={quality === "ok" ? "good" : quality === "unknown" ? "neutral" : "bad"}>{quality}</TerminalBadge>
            <TerminalBadge tone="info">read only</TerminalBadge>
          </div>
          <p className="mt-1 text-[12px] text-dim">Closed-hour structure · VWAP context · bounded AI narrative</p>
        </div>
        <div className="flex items-center gap-2">
          {SYMBOLS.map((item) => (
            <button
              key={item}
              onClick={() => { setSymbol(item); setSelected(null); }}
              className={`rounded-lg border px-3 py-2 font-mono text-[11px] transition ${symbol === item ? "border-brand/60 bg-brand/10 text-brand" : "border-line text-dim hover:text-txt"}`}
            >
              {item.replace("USDT", "")}
            </button>
          ))}
          <span className="hidden md:inline font-mono text-[11px] text-faint">{pulse.data?.as_of ? fullUtcHour(pulse.data.as_of) : "syncing"} UTC</span>
        </div>
      </section>

      {pulse.isError && (
        <div className="rounded-xl border border-short/40 bg-short/5 p-4 text-sm text-short">Pulse API unavailable. Canonical 1h data may still be warming.</div>
      )}

      <ScannerWorkspace
        lanes={lanes.data?.lanes ?? []}
        selectedSymbol={symbol}
        shadowPurse={lanes.data?.portfolio.shadow_purse_usd ?? null}
        streamState={pulse.streamState}
        risk={risk.data}
      />

      <section className="overflow-x-auto border border-line bg-panel/80" aria-label="All-symbol market monitor">
        <div className="grid min-w-[940px] grid-cols-[92px_140px_repeat(6,minmax(100px,1fr))] border-b border-line bg-inset/70 px-3 py-1.5 font-mono text-[9px] uppercase tracking-wide text-faint">
          <span>Instrument</span><span className="text-right">Last</span><span className="text-right">1h range</span><span className="text-right">vs VWAP</span><span className="text-right">Dual AVWAP</span><span className="text-right">Regime 1h</span><span className="text-right">vs prior POC</span><span className="text-right">Feed age</span>
        </div>
        {SYMBOLS.map((item, index) => {
          const data = marketQueries[index].data;
          const itemForming = data?.forming;
          const itemHours = data?.hours ?? [];
          const latestClosedHour = itemHours[itemHours.length - 1];
          const price = data?.market.mid ?? data?.market.last;
          const active = item === symbol;
          const itemQuality = data?.data_quality ?? "unknown";
          const feedAge = data?.market.feed_age_ms ?? data?.market.canonical_age_ms;
          return (
            <button key={item} onClick={() => { setSymbol(item); setSelected(null); }} className={`grid min-w-[940px] grid-cols-[92px_140px_repeat(6,minmax(100px,1fr))] items-center border-b border-line/70 px-3 py-2 text-left font-mono text-[10px] transition last:border-0 ${active ? "border-l-2 border-l-brand bg-brand/10" : "hover:bg-white/[0.025]"}`}>
              <span className="flex items-center gap-2 text-[11px] font-semibold text-txt">{item.replace("USDT", "")}<span className={`h-1.5 w-1.5 rounded-full ${itemQuality === "ok" ? "bg-long" : itemQuality === "unknown" ? "bg-faint" : "bg-short"}`} /></span>
              <span className="text-right text-[13px] tabular-nums text-txt">{priceText(price)}</span>
              <span className="text-right">{fmt(latestClosedHour?.range_bps)} bps</span>
              <span className="text-right">{signed((itemForming?.vs_session_vwap_bps as number | undefined) ?? data?.indicators.vs_session_vwap_bps)} bps</span>
              <span className="text-right">{itemForming?.dual_avwap_bias ?? data?.indicators.dual_avwap_bias ?? "n/a"}</span>
              <span className="text-right">{data?.regime?.["1h"].label?.replace(/_/g, " ") ?? "unavailable"}</span>
              <span className="text-right">{signed(data?.volume_profile.prior_day.vs_poc_bps)} bps</span>
              <span className="text-right">{feedAge == null ? "not reported" : ageSecMs(feedAge)}</span>
            </button>
          );
        })}
      </section>

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1.7fr)_minmax(300px,.7fr)] gap-4">
        <TerminalPanel title="1h market structure" meta={`${hours.length} closed hours · ${symbol}`}>
          <CandleChart
            hours={hours}
            forming={forming}
            asOf={pulse.data?.as_of}
            selected={selectedHour?.open_time ?? null}
            avwap={pulse.data?.indicators.avwap}
            avwapLabel={pulse.data?.indicators.avwap_label}
            priorDayPoc={priorDayProfile?.poc}
            priorDayVah={priorDayProfile?.value_area_high}
            priorDayVal={priorDayProfile?.value_area_low}
          />
        </TerminalPanel>

        <div className="flex flex-col gap-4">
          <TerminalPanel title="This hour" meta="forming · never persisted">
            <div className="flex items-center justify-between mb-4">
              <div>
                <div className="text-[11px] font-mono text-faint">LIVE MID</div>
                <div className="text-3xl font-mono tabular-nums">{priceText((forming?.mid as number | null | undefined) ?? book?.mid)}</div>
              </div>
              <TerminalBadge tone={formingActive ? "info" : "neutral"}>{formingActive ? "forming" : "awaiting trades"}</TerminalBadge>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <Metric label="Range" value={`${fmt(formingRange)} bps`} note={formingRangeVsMedian == null ? "24h median unavailable" : `${fmt(formingRangeVsMedian, 2)}× 24h median`} />
              <Metric label="Volume" value={formingVolumeRank == null ? "rank —" : `${Math.round(formingVolumeRank * 100)}th pct`} note={formingVolumeVsMedian == null ? "24h median unavailable" : `${fmt(formingVolumeVsMedian, 2)}× 24h median`} />
              <Metric label="vs session VWAP" value={`${signed((forming?.vs_session_vwap_bps as number | undefined) ?? pulse.data?.indicators.vs_session_vwap_bps)} bps`} />
              <Metric label="Dual AVWAP" value={forming?.dual_avwap_bias ?? pulse.data?.indicators.dual_avwap_bias ?? "n/a"} note={dualAvwapNote ?? undefined} />
              <Metric label="Regime 1h / 4h" value={`${pulse.data?.regime?.["1h"].label.replace(/_/g, " ") ?? "unavailable"} / ${pulse.data?.regime?.["4h"].label.replace(/_/g, " ") ?? "unavailable"}`} note="closed bars · measurement only" />
              <Metric label="Prior-day POC" value={priorDayProfile?.poc == null ? "unavailable" : priceText(priorDayProfile.poc)} note={priorDayProfile?.available ? `${signed(priorDayProfile.vs_poc_bps)} bps · trade-derived` : profileReason} />
              <Metric label="VAL / VAH" value={priorDayProfile?.available ? `${priceText(priorDayProfile.value_area_low)} – ${priceText(priorDayProfile.value_area_high)}` : "unavailable"} note={priorDayProfile?.available ? `${priorDayProfile.location.replace(/_/g, " ")} · ${((priorDayProfile.va_volume_pct ?? 0) * 100).toFixed(1)}% volume` : profileReason} />
              <Metric label="Session" value={(forming?.session_label as string | undefined) ?? pulse.data?.market.session_label ?? "—"} note={((forming?.session_active as boolean | undefined) ?? pulse.data?.market.session_label === "us_overlap") ? "active overlap" : "off overlap"} />
              <Metric label="Quality" value={quality} note={qualityNote} />
            </div>
          </TerminalPanel>

          <TerminalPanel title={`AI brief${selectedHour ? ` · ${symbol.replace("USDT", "")}` : ""}`} meta={selectedHour ? `${utcHour(selectedHour.open_time)}:00–${utcHour(selectedHour.close_time)}:00 UTC` : "select an hour"}>
            {analysis.isLoading ? (
              <div className="py-8 text-center font-mono text-xs text-dim">Building bounded observation…</div>
            ) : analysis.data ? (
              <div className={`space-y-4 rounded-lg border p-4 ${analysis.data.data_quality === "ok" ? "border-line bg-inset/50" : "border-short/50 bg-short/5"}`}>
                <div className="flex items-center justify-between gap-3">
                  <div className={`font-mono text-sm font-semibold uppercase tracking-[0.12em] ${analysis.data.data_quality === "ok" ? "text-brand" : "text-short"}`}>
                    {analysis.data.sections.state.label}
                  </div>
                  <TerminalBadge tone={analysis.data.data_quality === "ok" ? "neutral" : "bad"}>{analysis.data.data_quality}</TerminalBadge>
                </div>
                <p className="text-[13px] leading-5 text-txt/90">{analysis.data.sections.state.summary}</p>

                <div>
                  <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-brand">What mattered</div>
                  <ul className="mt-1 space-y-1 text-[13px] leading-5 text-txt/90">
                    {analysis.data.sections.what_mattered.bullets.map((bullet) => <li key={bullet}>• {bullet}</li>)}
                  </ul>
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-brand">Structure</div>
                    <TerminalBadge tone="neutral">{analysis.data.sections.structure.bias_tag}</TerminalBadge>
                  </div>
                  <p className="mt-1 text-[13px] leading-5 text-txt/90">{analysis.data.sections.structure.summary}</p>
                </div>
                <div>
                  <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-brand">Risks</div>
                  <ul className="mt-1 space-y-1 text-[13px] leading-5 text-txt/90">
                    {analysis.data.sections.risks.bullets.map((bullet) => <li key={bullet}>• {bullet}</li>)}
                  </ul>
                </div>
                <div>
                  <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-brand">Watch next</div>
                  <p className="mt-1 text-[13px] leading-5 text-txt/90">{analysis.data.sections.watch_next.summary}</p>
                </div>
                <div className="border-t border-line pt-3 font-mono text-[10px] text-faint">{analysis.data.disclaimer}</div>
              </div>
            ) : (
              <div className="py-8 text-center font-mono text-xs text-dim">No closed-hour brief available.</div>
            )}
          </TerminalPanel>
        </div>
      </div>

      <TerminalPanel title="Hour strip" meta="click an hour to lock its brief">
        <div className="flex gap-2 overflow-x-auto pb-2">
          {hours.map((hour) => {
            const up = hour.close_vs_open_bps >= 0;
            const active = selectedHour?.open_time === hour.open_time;
            const intensity = Math.min(1, Math.max(0.18, hour.range_bps / 120));
            return (
              <button
                key={hour.open_time}
                onClick={() => setSelected(hour.open_time)}
                className={`min-w-[112px] rounded-lg border p-3 text-left transition ${active ? "border-brand bg-brand/10" : "border-line bg-inset hover:border-line2"}`}
                style={hour.is_gap ? { backgroundImage: "repeating-linear-gradient(135deg, transparent, transparent 7px, rgba(248,81,73,.08) 7px, rgba(248,81,73,.08) 14px)" } : undefined}
              >
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[11px] text-dim">{utcHour(hour.open_time)} UTC</span>
                  {hour.data_quality !== "ok" && <span className="text-short text-[10px]">GAP</span>}
                </div>
                <div className={`mt-2 h-1.5 rounded-full ${up ? "bg-long" : "bg-short"}`} style={{ opacity: intensity }} />
                <div className={`mt-2 font-mono text-sm ${up ? "text-long" : "text-short"}`}>{signed(hour.close_vs_open_bps)} bps</div>
                <div className="mt-1 font-mono text-[10px] text-faint">range {fmt(hour.range_bps)} · vol {fmt(hour.volume_vs_median_20h, 1)}×</div>
              </button>
            );
          })}
        </div>
      </TerminalPanel>

      <TerminalPanel title="Incident rail" meta={`${activeEventCount} active · hour closes · volume · integrity`}>
        <div className="mb-3 flex items-center gap-1 border-b border-line pb-2" role="group" aria-label="Incident filter">
          {(["active", "all", "recovered"] as const).map((filter) => <button key={filter} onClick={() => setAlertFilter(filter)} className={`border px-2 py-1 font-mono text-[9px] uppercase ${alertFilter === filter ? "border-brand/60 bg-brand/10 text-brand" : "border-line text-dim"}`}>{filter}</button>)}
          <span className="ml-auto font-mono text-[9px] text-faint">read-only · recovery evidence retained</span>
        </div>
        <div className="divide-y divide-line/70">
          {visibleEvents.slice(0, 20).map((alert, index) => (
            <div key={`${alert.at}-${index}`} className="flex items-start gap-3 py-3 first:pt-0 last:pb-0">
              <span className={`mt-1 h-2 w-2 rounded-full ${alert.severity === "critical" ? "bg-short" : alert.severity === "warning" ? "bg-warn" : "bg-info"}`} />
              <div className="min-w-0 flex-1">
                <div className="text-[12px] text-txt">{alert.message}</div>
                <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-faint"><span>{fullUtcHour(alert.at)} UTC · {alert.kind}</span><TerminalBadge tone={alert.recovered ? "neutral" : alert.severity === "critical" ? "bad" : "warn"}>{alert.recovered ? "recovered" : "active"}</TerminalBadge></div>
              </div>
            </div>
          ))}
          {!visibleEvents.length && <div className="py-4 text-[12px] text-dim">No incidents match the current filter.</div>}
        </div>
      </TerminalPanel>
    </div>
  );
}
