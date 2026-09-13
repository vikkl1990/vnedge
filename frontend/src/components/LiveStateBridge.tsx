import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Snapshot } from "../api";
import { useSnapshot } from "../queries";
import { acceptSnapshot, updatesFresh, LIVE_STALE_MS } from "../liveConnection";

/** One authenticated read-only stream for all global cockpit consumers. */
export function LiveStateBridge() {
  const queryClient = useQueryClient();
  const { data, dataUpdatedAt } = useSnapshot();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    let socket: WebSocket | null = null;
    let stopped = false;
    let retryTimer: number | null = null;
    let attempt = 0;
    let lastMessageAt = Date.now();
    const connect = () => {
      if (stopped) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
      lastMessageAt = Date.now();
      socket.onopen = () => {
        void queryClient.invalidateQueries({ queryKey: ["correction-lanes"] });
      };
      socket.onmessage = (event) => {
        try {
          const incoming: unknown = JSON.parse(event.data);
          if (!acceptSnapshot(incoming, queryClient.getQueryData(["state"]), Date.now())) return;
          lastMessageAt = Date.now();
          attempt = 0;
          queryClient.setQueryData<Snapshot>(["state"], incoming as Snapshot);
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
    const timer = window.setInterval(() => {
      setNow(Date.now());
      // Half-open sockets need an application timeout; onclose alone never
      // fires when a proxy/network silently drops frames. REST stays active.
      if (socket && socket.readyState < WebSocket.CLOSING && Date.now() - lastMessageAt > LIVE_STALE_MS) socket.close();
    }, 5_000);
    const refresh = () => {
      if (document.visibilityState === "visible") {
        void queryClient.invalidateQueries({ queryKey: ["state"] });
        void queryClient.invalidateQueries({ queryKey: ["correction-lanes"] });
      }
    };
    window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      window.removeEventListener("online", refresh);
      document.removeEventListener("visibilitychange", refresh);
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      socket?.close();
    };
  }, [queryClient]);
  const fresh = updatesFresh(data, dataUpdatedAt, now);
  return <div role="status" className={`px-4 py-1 text-[11px] font-mono ${fresh ? "text-long" : "text-warn border-b border-warn/40"}`}>
    {fresh ? "LIVE UPDATES · measurement only — not trading permission" : "UPDATES STALE / CONNECTING · automatic reconnect active — displayed values may be old"}
  </div>;
}
