// Scanner chart: canonical candles rendered by Vela (own-bars, fully offline)
// with the lanes' actual scanner events drawn on the price they refer to —
// entry/stop/target lines, the risk zone, and fire markers — plus a trade-plan
// card column. Presentation only: every number here is read back from journals
// and the canonical lake; nothing on this panel informs a decision.
//
// Data policy (deliberate): Vela's bundled exchange providers are NEVER
// registered. Bars come only from /api/candles (the canonical lake), so the
// operator sees the same series research and shadow read — not a fourth
// candle source invented for the UI.

import { useEffect, useMemo, useRef, useState } from "react";
import { Vela } from "@luxalgo/vela";
import { useQuery } from "@tanstack/react-query";
import {
  fetchChartCandles,
  fetchChartMarkers,
  type ChartTimeframe,
  type ScannerAuditEvent,
} from "../api";
import { useJournal } from "../queries";
import { TerminalBadge, TerminalPanel } from "./Terminal";

const SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"] as const;
const TIMEFRAMES: ChartTimeframe[] = ["5m", "15m", "1h", "4h"];
const OVERLAY_PLANS = 3;

const COLORS = {
  entry: "#60a5fa",
  stop: "#f87171",
  target: "#34d399",
  fireLong: "#34d399",
  fireShort: "#f87171",
};

interface Plan {
  key: string;
  ts_ms: number;
  side: string;
  entry: number;
  stop: number;
  target: number | null;
  approved: boolean;
  reason: string;
  strategy_id: string;
  kind: string;
}

function toPlans(
  events: ScannerAuditEvent[] | undefined,
  symbol: string,
  timeframe: string,
): Plan[] {
  if (!events) return [];
  const plans: Plan[] = [];
  for (const event of events) {
    if (event.symbol !== symbol) continue;
    if (event.timeframe && event.timeframe !== timeframe) continue;
    if (event.kind !== "signal" && event.kind !== "entry") continue;
    const entry = event.entry_price ?? event.decision_price ?? event.price;
    const stop = event.stop_price;
    if (typeof entry !== "number" || typeof stop !== "number") continue;
    const ts_ms = Date.parse(event.bar_ts || event.ts);
    if (!Number.isFinite(ts_ms)) continue;
    plans.push({
      key: `${event.intent_key || event.ts}-${event.kind}`,
      ts_ms,
      side: event.side,
      entry,
      stop,
      target: typeof event.target_price === "number" ? event.target_price : null,
      approved: event.approved,
      reason: event.reason,
      strategy_id: event.strategy_id,
      kind: event.kind,
    });
  }
  plans.sort((a, b) => b.ts_ms - a.ts_ms);
  return plans;
}

const price = (value: number | null | undefined) =>
  typeof value === "number" && Number.isFinite(value)
    ? new Intl.NumberFormat("en-US", {
        maximumFractionDigits: value >= 1_000 ? 2 : 4,
      }).format(value)
    : "—";

const riskBps = (plan: Plan) =>
  plan.entry > 0 ? Math.abs((plan.entry - plan.stop) / plan.entry) * 1e4 : null;

export function ScannerChart() {
  const [symbol, setSymbol] = useState<(typeof SYMBOLS)[number]>(SYMBOLS[0]);
  const [timeframe, setTimeframe] = useState<ChartTimeframe>("15m");
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<InstanceType<typeof Vela> | null>(null);
  const drawnIdsRef = useRef<string[]>([]);

  const candles = useQuery({
    queryKey: ["scanner-chart-candles", symbol, timeframe],
    queryFn: () => fetchChartCandles(symbol, timeframe, 500),
    refetchInterval: 30_000,
  });
  const markers = useQuery({
    queryKey: ["scanner-chart-markers", symbol],
    queryFn: () => fetchChartMarkers(symbol, 200),
    refetchInterval: 30_000,
  });
  const journal = useJournal(150, 0);

  const bars = useMemo(
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

  const plans = useMemo(
    () => toPlans(journal.data?.scanner_events, symbol, timeframe),
    [journal.data, symbol, timeframe],
  );

  // Chart lifecycle: create once, then update the market in place.
  useEffect(() => {
    if (!containerRef.current || bars.length === 0) return;
    if (!chartRef.current) {
      chartRef.current = new Vela(containerRef.current, {
        data: bars,
        timeframe,
        theme: "dark",
      });
      chartRef.current.drawings?.showToolbar?.(false);
      return;
    }
    void chartRef.current.setMarket({ data: bars, timeframe });
  }, [bars, timeframe]);

  useEffect(
    () => () => {
      chartRef.current?.destroy?.();
      chartRef.current = null;
      drawnIdsRef.current = [];
    },
    [],
  );

  // Overlays: markers from the lanes' fills, plan lines from scanner events.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || bars.length === 0) return;
    const drawings = chart.drawings;
    if (!drawings?.add) return;

    for (const id of drawnIdsRef.current) {
      try {
        drawings.remove?.(id);
      } catch {
        /* a resize/reload may have dropped it already */
      }
    }
    drawnIdsRef.current = [];
    const lastTime = bars[bars.length - 1].time;
    const firstTime = bars[0].time;
    const keep = (drawing: { id: string } | null | undefined) => {
      if (drawing?.id) drawnIdsRef.current.push(drawing.id);
    };
    const clampTime = (ms: number) =>
      Math.min(Math.max(ms, firstTime), lastTime);
    const barByTime = new Map(bars.map((bar) => [bar.time, bar]));

    for (const marker of markers.data?.markers ?? []) {
      const time = marker.time * 1000;
      const bar = barByTime.get(time);
      if (!bar) continue; // marker outside the loaded window
      const price = marker.position === "aboveBar" ? bar.high : bar.low;
      try {
        keep(
          drawings.add(
            marker.shape === "arrowDown" ? "arrowmarkdown" : "arrowmarkup",
            { paneId: "price", anchors: [{ time, price }] },
          ),
        );
      } catch {
        /* renderer rejected the mark: skip */
      }
    }

    for (const plan of plans.slice(0, OVERLAY_PLANS)) {
      const t0 = clampTime(plan.ts_ms);
      try {
        const zone = drawings.add("box", {
          paneId: "price",
          anchors: [
            { time: t0, price: plan.stop },
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
              { time: t0, price: level },
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
        /* plan predates the loaded window: skip its overlay */
      }
    }
  }, [bars, markers.data, plans]);

  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_320px]">
      <TerminalPanel
        title={`Scanner chart · ${symbol} · ${timeframe}`}
        meta="canonical lake bars · journal overlays · Vela renderer"
      >
        <div className="mb-2 flex items-center gap-2 flex-wrap">
          {SYMBOLS.map((item) => (
            <button
              key={item}
              onClick={() => setSymbol(item)}
              className={`rounded border px-2 py-1 text-[11px] font-mono ${
                item === symbol
                  ? "border-brand text-brand"
                  : "border-line text-dim hover:text-txt"
              }`}
            >
              {item.split("/")[0]}
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
          <span className="ml-auto text-[10px] font-mono text-faint">
            {candles.data
              ? `${candles.data.count} bars · ${candles.data.source}`
              : candles.isError
                ? "candles unavailable"
                : "loading…"}
          </span>
        </div>
        <div
          ref={containerRef}
          className="h-[520px] w-full rounded border border-line bg-black/20"
        />
      </TerminalPanel>

      <TerminalPanel title="Trade plans" meta="latest scanner fires · read-only">
        <div className="flex flex-col gap-2">
          {plans.length === 0 && (
            <div className="text-[11px] font-mono text-dim">
              No scanner events for {symbol} @ {timeframe} in the journal tail.
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
                  <span
                    className={plan.side === "long" ? "text-long" : "text-short"}
                  >
                    {plan.side.toUpperCase()} · {plan.strategy_id}
                  </span>
                  <TerminalBadge tone={plan.approved ? "good" : "warn"}>
                    {plan.approved ? "approved" : plan.kind}
                  </TerminalBadge>
                </div>
                <div className="mt-1 grid grid-cols-3 gap-1 text-dim">
                  <span>
                    E <span className="text-txt">{price(plan.entry)}</span>
                  </span>
                  <span>
                    SL <span className="text-short">{price(plan.stop)}</span>
                  </span>
                  <span>
                    TP <span className="text-long">{price(plan.target)}</span>
                  </span>
                </div>
                <div className="mt-1 flex items-center justify-between text-faint">
                  <span>{risk !== null ? `${risk.toFixed(1)} bps to stop` : "—"}</span>
                  <span>{new Date(plan.ts_ms).toISOString().slice(5, 16)}</span>
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
