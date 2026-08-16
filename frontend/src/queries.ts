// TanStack Query hooks over the existing endpoints — caching, dedup, and
// interval refetch for free, with zero backend change. The classic dashboard
// hand-rolls setInterval polling; this replaces that with declarative queries.

import { useQuery } from "@tanstack/react-query";
import {
  apiGet,
  type HourBrief,
  type JournalRow,
  type LanesPayload,
  type PulsePayload,
  type RiskSnapshot,
  type Snapshot,
  type WhoAmI,
} from "./api";

export function useWhoAmI() {
  return useQuery({
    queryKey: ["whoami"],
    queryFn: () => apiGet<WhoAmI>("/whoami"),
    staleTime: 60_000,
  });
}

export function useSnapshot() {
  return useQuery({
    queryKey: ["state"],
    queryFn: () => apiGet<Snapshot>("/state"),
    refetchInterval: 5_000,
  });
}

export function useLanes() {
  return useQuery({
    queryKey: ["correction-lanes"],
    queryFn: () => apiGet<LanesPayload>("/api/lanes"),
    refetchInterval: 5_000,
  });
}

export function useRiskSnapshot() {
  return useQuery({
    queryKey: ["correction-risk"],
    queryFn: () => apiGet<RiskSnapshot>("/api/risk/snapshot"),
    refetchInterval: 5_000,
  });
}

export function useJournal(limit = 50) {
  return useQuery({
    queryKey: ["journal", limit],
    // the route is /trade-journal (not /journal), and it returns a projection
    // object — the closed-trade rows live under `closed_trades`.
    queryFn: async () => {
      const r = await apiGet<{ closed_trades?: JournalRow[] }>(`/trade-journal?limit=${limit}`);
      return r.closed_trades ?? [];
    },
    refetchInterval: 20_000,
  });
}

export function usePulse(symbol: string, exchange = "binanceusdm") {
  return useQuery({
    queryKey: ["pulse", exchange, symbol],
    queryFn: () =>
      apiGet<PulsePayload>(
        `/api/pulse/${encodeURIComponent(symbol)}?exchange=${encodeURIComponent(exchange)}&n=48`,
      ),
    refetchInterval: 10_000,
  });
}

export function useHourAnalysis(
  symbol: string,
  openTime: string | null,
  exchange = "binanceusdm",
) {
  return useQuery({
    queryKey: ["pulse-analysis", exchange, symbol, openTime],
    queryFn: () =>
      apiGet<HourBrief>(
        `/api/pulse/${encodeURIComponent(symbol)}/hours/${encodeURIComponent(openTime!)}/analysis?exchange=${encodeURIComponent(exchange)}`,
      ),
    enabled: Boolean(openTime),
    staleTime: Number.POSITIVE_INFINITY,
  });
}
