import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PulseHour } from "../api";
import { useHourAnalysis, usePulse } from "../queries";
import { TerminalBadge, TerminalPanel } from "./Terminal";

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];

const fmt = (value: number | null | undefined, digits = 1) =>
  typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";

const signed = (value: number | null | undefined, digits = 1) =>
  typeof value === "number" && Number.isFinite(value)
    ? `${value > 0 ? "+" : ""}${value.toFixed(digits)}`
    : "—";

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
  for (const rawTime of timeline) {
    const time = rawTime as UTCTimestamp;
    const hour = byTime.get(rawTime);
    if (!hour) {
      candles.push({ time });
      vwap.push({ time });
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
  }
  return {
    candles,
    vwap,
    signature: hours
      .map((hour) => [
        hour.symbol,
        hour.open_time,
        hour.open,
        hour.high,
        hour.low,
        hour.close,
        hour.session_vwap,
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
}: {
  hours: PulseHour[];
  forming: Record<string, unknown> | null | undefined;
  asOf: string | undefined;
  selected: string | null;
  avwap: number | null | undefined;
  avwapLabel: string | null | undefined;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vwapRef = useRef<ISeriesApi<"Line"> | null>(null);
  const avwapRef = useRef<IPriceLine | null>(null);
  const historySignatureRef = useRef("");
  const formingTimeRef = useRef<UTCTimestamp | null>(null);
  const fittedRef = useRef(false);
  const [rendererError, setRendererError] = useState<string | null>(null);
  const points = useMemo(() => chartPoints(hours), [hours]);
  const livePoint = useMemo(() => formingPoint(forming, asOf), [forming, asOf]);
  const hasData = hours.length > 0 || livePoint !== null;
  const hasDegradedHours = hours.some((hour) => hour.data_quality !== "ok");

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
      container.dataset.chartState = "ready";
      chartRef.current = chart;
      candleRef.current = candles;
      vwapRef.current = vwap;

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
        avwapRef.current = null;
        historySignatureRef.current = "";
        formingTimeRef.current = null;
        fittedRef.current = false;
      };
    } catch (error) {
      container.dataset.chartState = "error";
      setRendererError(error instanceof Error ? error.message : "unknown chart error");
      return undefined;
    }
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    const candles = candleRef.current;
    const vwap = vwapRef.current;
    if (!chart || !candles || !vwap) return;

    const historyChanged = historySignatureRef.current !== points.signature;
    const formingRolled = (
      formingTimeRef.current !== null
      && livePoint !== null
      && formingTimeRef.current !== livePoint.time
    );
    const formingCleared = formingTimeRef.current !== null && livePoint === null;
    if (historyChanged || formingRolled || formingCleared) {
      candles.setData(points.candles);
      vwap.setData(points.vwap);
      historySignatureRef.current = points.signature;
      if (!fittedRef.current && points.candles.length > 0) {
        chart.timeScale().fitContent();
        fittedRef.current = true;
      }
    }
    if (livePoint) candles.update(livePoint);
    formingTimeRef.current = livePoint?.time ?? null;
  }, [livePoint, points]);

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

  return (
    <div className="relative overflow-hidden rounded-md bg-inset">
      <div
        ref={containerRef}
        className="h-[360px] w-full"
        role="img"
        aria-label={`${hours.length} closed one-hour candlesticks with session VWAP${livePoint ? " and one forming hour" : ""}. Times are UTC.`}
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
        {livePoint && <span className="flex items-center gap-1.5 text-info"><span className="h-2 w-2 bg-info" />FORMING</span>}
        {hasDegradedHours && <span className="text-faint">MUTED = GAP/DEGRADED</span>}
      </div>
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
  const pulse = usePulse(symbol);
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

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1.7fr)_minmax(300px,.7fr)] gap-4">
        <TerminalPanel title="1h market structure" meta={`${hours.length} closed hours · ${symbol}`}>
          <CandleChart
            hours={hours}
            forming={forming}
            asOf={pulse.data?.as_of}
            selected={selectedHour?.open_time ?? null}
            avwap={pulse.data?.indicators.avwap}
            avwapLabel={pulse.data?.indicators.avwap_label}
          />
        </TerminalPanel>

        <div className="flex flex-col gap-4">
          <TerminalPanel title="This hour" meta="forming · never persisted">
            <div className="flex items-center justify-between mb-4">
              <div>
                <div className="text-[11px] font-mono text-faint">LIVE MID</div>
                <div className="text-3xl font-mono tabular-nums">{book?.mid?.toLocaleString() ?? "—"}</div>
              </div>
              <TerminalBadge tone={forming ? "info" : "neutral"}>{forming ? "in progress" : "awaiting trades"}</TerminalBadge>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <Metric label="Range" value={`${fmt(forming?.range_bps as number | undefined)} bps`} />
              <Metric label="Volume / 20h" value={`${fmt(forming?.volume_vs_median_20h as number | undefined, 2)}×`} />
              <Metric label="vs session VWAP" value={`${signed(pulse.data?.indicators.vs_session_vwap_bps)} bps`} />
              <Metric label="Dual AVWAP" value={pulse.data?.indicators.dual_avwap_bias ?? "—"} />
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

      <TerminalPanel title="Notifications" meta="hour closes · volume · integrity">
        <div className="divide-y divide-line/70">
          {(pulse.data?.alerts ?? []).slice(0, 10).map((alert, index) => (
            <div key={`${alert.at}-${index}`} className="flex items-start gap-3 py-3 first:pt-0 last:pb-0">
              <span className={`mt-1 h-2 w-2 rounded-full ${alert.severity === "critical" ? "bg-short" : alert.severity === "warning" ? "bg-warn" : "bg-info"}`} />
              <div className="min-w-0 flex-1">
                <div className="text-[12px] text-txt">{alert.message}</div>
                <div className="mt-1 font-mono text-[10px] text-faint">{fullUtcHour(alert.at)} UTC · {alert.kind}</div>
              </div>
            </div>
          ))}
          {!pulse.data?.alerts.length && <div className="py-4 text-[12px] text-dim">No pulse events yet.</div>}
        </div>
      </TerminalPanel>
    </div>
  );
}
