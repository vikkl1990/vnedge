import { useEffect, useRef, useState } from "react";
import { VelaWorkspace } from "@luxalgo/vela/workspace";
import type { MarketChoice } from "./ScannerChart";
import {
  canonicalChartSymbol,
  VnedgeDataProvider,
} from "../vela/VnedgeDataProvider";

const CLOCKS = [
  ["regime", "4h", "REGIME"],
  ["structure", "1h", "STRUCTURE"],
  ["setup", "15m", "SETUP"],
  ["trigger", "5m", "TRIGGER"],
] as const;

const errorText = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

/** Four causal clocks over one canonical provider; presentation-only. */
export function TelescopeWorkspace({ market }: { market: MarketChoice | null }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!hostRef.current || !market) return;
    const provider = new VnedgeDataProvider({
      exchange: market.exchange,
      symbol: market.symbol,
      label: market.label,
    });
    const symbol = `vnedge:${canonicalChartSymbol(market.symbol)}`;
    let workspace: VelaWorkspace | null = null;
    try {
      workspace = new VelaWorkspace(hostRef.current, {
        layout: "4",
        symbol,
        timeframe: "15m",
        bars: 320,
        live: true,
        theme: "dark",
        volume: true,
        persist: false,
        drawingToolbar: false,
        providers: { vnedge: () => provider },
        cells: Object.fromEntries(
          CLOCKS.map(([id, timeframe]) => [id, { symbol, timeframe, bars: 320 }]),
        ),
        sync: { crosshair: true, viewport: true, style: true },
      });
      setError(null);
    } catch (caught) {
      setError(errorText(caught));
    }
    return () => {
      workspace?.destroy();
      provider.destroy();
    };
  }, [market?.exchange, market?.key, market?.label, market?.symbol]);

  if (!market) {
    return (
      <div className="flex h-[720px] items-center justify-center rounded border border-line text-[11px] font-mono text-dim">
        No active market is available for telescope view.
      </div>
    );
  }

  return (
    <div className="relative">
      <div className="mb-2 grid grid-cols-4 gap-2">
        {CLOCKS.map(([, timeframe, role]) => (
          <div key={role} className="rounded border border-line bg-bg/60 px-2 py-1 font-mono">
            <span className="text-[10px] text-brand">{timeframe.toUpperCase()}</span>
            <span className="ml-2 text-[10px] text-dim">{role}</span>
          </div>
        ))}
      </div>
      <div
        ref={hostRef}
        className="h-[720px] w-full overflow-hidden rounded border border-line bg-black/20"
      />
      {error && (
        <div className="pointer-events-none absolute inset-x-4 top-14 rounded border border-short/40 bg-bg/90 px-3 py-2 text-[11px] font-mono text-short">
          Telescope renderer: {error}
        </div>
      )}
      <div className="mt-2 text-[10px] font-mono text-faint">
        Last closed bars only · 4h permits · 1h structures · 15m sets up · 5m triggers · synchronized crosshair
      </div>
    </div>
  );
}
