// TanStack Query hooks over the existing endpoints — caching, dedup, and
// interval refetch for free, with zero backend change. The classic dashboard
// hand-rolls setInterval polling; this replaces that with declarative queries.

import { useQuery } from "@tanstack/react-query";
import { apiGet, type JournalRow, type Snapshot, type WhoAmI } from "./api";

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
