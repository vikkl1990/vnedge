import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Snapshot } from "../api";

/** One authenticated read-only stream for all global cockpit consumers. */
export function LiveStateBridge() {
  const queryClient = useQueryClient();
  useEffect(() => {
    let socket: WebSocket | null = null;
    let stopped = false;
    let retryTimer: number | null = null;
    let attempt = 0;
    const connect = () => {
      if (stopped) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
      socket.onopen = () => { attempt = 0; };
      socket.onmessage = (event) => {
        try {
          queryClient.setQueryData<Snapshot>(["state"], JSON.parse(event.data) as Snapshot);
        } catch {
          // Preserve last-known-good state; REST polling remains the fallback.
        }
      };
      socket.onclose = () => {
        if (stopped) return;
        attempt += 1;
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
  }, [queryClient]);
  return null;
}
