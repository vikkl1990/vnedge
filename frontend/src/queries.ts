// TanStack Query hooks over the existing endpoints — caching, dedup, and
// interval refetch for free, with zero backend change. The classic dashboard
// hand-rolls setInterval polling; this replaces that with declarative queries.

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiGet,
  type HourBrief,
  type CostModelPayload,
  type JournalPayload,
  type LanesPayload,
  type PulsePayload,
  type RiskSnapshot,
  type ResearchScorecard,
  type Snapshot,
  type MetaPayload,
  type MlStatus,
  type AgenticResearchStatus,
  type WhoAmI,
  type SettingsSecurity,
  type OperatorProfile,
  type ExchangeConnectionPublic,
} from "./api";

export function useWhoAmI() {
  return useQuery({
    queryKey: ["whoami"],
    queryFn: () => apiGet<WhoAmI>("/whoami"),
    staleTime: 60_000,
  });
}

export function useSettingsSecurity() {
  return useQuery({
    queryKey: ["settings-security"],
    queryFn: () => apiGet<SettingsSecurity>("/api/settings/security"),
    staleTime: 60_000,
  });
}

export function useOperatorProfile() {
  return useQuery({
    queryKey: ["settings-profile"],
    queryFn: () => apiGet<OperatorProfile>("/api/settings/profile"),
    staleTime: 60_000,
  });
}

export function useExchangeConnections() {
  return useQuery({
    queryKey: ["settings-exchanges"],
    queryFn: () => apiGet<ExchangeConnectionPublic[]>("/api/settings/exchanges"),
    staleTime: 30_000,
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
    queryFn: () => apiGet<JournalPayload>(`/trade-journal?limit=${limit}`),
    refetchInterval: 20_000,
  });
}

export function useMeta() {
  return useQuery({
    queryKey: ["meta"],
    queryFn: () => apiGet<MetaPayload>("/meta"),
    staleTime: 60_000,
  });
}

export function useCostModel() {
  return useQuery({
    queryKey: ["cost-model"],
    queryFn: () => apiGet<CostModelPayload>("/cost-model"),
    staleTime: 60_000,
  });
}

export function useResearchScorecard() {
  return useQuery({
    queryKey: ["scorecard"],
    queryFn: () => apiGet<ResearchScorecard>("/scorecard"),
    staleTime: 60_000,
  });
}

export function useMlStatus() {
  return useQuery({
    queryKey: ["ml-status"],
    queryFn: () => apiGet<MlStatus>("/ml-status"),
    refetchInterval: 60_000,
  });
}

export function useAgenticResearchStatus() {
  return useQuery({
    queryKey: ["agentic-research-os"],
    queryFn: () => apiGet<AgenticResearchStatus>("/agentic-research-os"),
    refetchInterval: 60_000,
  });
}

export function usePulse(symbol: string, exchange = "binanceusdm") {
  const queryClient = useQueryClient();
  const [streamState, setStreamState] = useState<"connecting" | "live" | "retrying">("connecting");
  const query = useQuery({
    queryKey: ["pulse", exchange, symbol],
    queryFn: () =>
      apiGet<PulsePayload>(
        `/api/pulse/${encodeURIComponent(symbol)}?exchange=${encodeURIComponent(exchange)}&n=48`,
      ),
    refetchInterval: 10_000,
  });

  useEffect(() => {
    let socket: WebSocket | null = null;
    let stopped = false;
    let retryTimer: number | null = null;
    let attempt = 0;

    const connect = () => {
      if (stopped) return;
      setStreamState(attempt === 0 ? "connecting" : "retrying");
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${protocol}//${window.location.host}/api/pulse/stream?symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}`;
      socket = new WebSocket(url);
      socket.onopen = () => {
        attempt = 0;
        setStreamState("live");
      };
      socket.onmessage = (event) => {
        try {
          const incoming = JSON.parse(event.data) as PulsePayload;
          queryClient.setQueryData<PulsePayload>(["pulse", exchange, symbol], (current) => {
            if (!current) return incoming;
            const currentAt = Date.parse(current.as_of);
            const incomingAt = Date.parse(incoming.as_of);
            return Number.isFinite(incomingAt) && incomingAt >= currentAt ? incoming : current;
          });
        } catch {
          // A malformed frame cannot invalidate the last known-good REST state.
        }
      };
      socket.onclose = () => {
        if (stopped) return;
        attempt += 1;
        setStreamState("retrying");
        retryTimer = window.setTimeout(connect, Math.min(30_000, 1_000 * 2 ** Math.min(attempt, 5)));
      };
      socket.onerror = () => socket?.close();
    };

    connect();
    return () => {
      stopped = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      socket?.close();
    };
  }, [exchange, queryClient, symbol]);

  return { ...query, streamState };
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
