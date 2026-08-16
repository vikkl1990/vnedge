import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiDelete, apiPost, apiPut, type ExchangeConnectionPublic, type ExchangeId } from "../../api";
import { useExchangeConnections } from "../../queries";
import { RotateKeysModal } from "./RotateKeysModal";

const LABELS: Record<ExchangeId, string> = {
  binanceusdm: "Binance USD-M",
  bybit: "Bybit",
  delta_india: "Delta India",
};

const tone: Record<ExchangeConnectionPublic["status"], string> = {
  not_configured: "border-line text-dim",
  configured: "border-warn/40 text-warn",
  verified: "border-long/40 text-long",
  invalid: "border-short/40 text-short",
  disabled: "border-line text-faint",
};

export function ExchangesPanel({ secretsReady }: { secretsReady: boolean }) {
  const connections = useExchangeConnections();
  const queryClient = useQueryClient();
  const [rotate, setRotate] = useState<ExchangeId | null>(null);
  const [busy, setBusy] = useState<ExchangeId | null>(null);
  const [message, setMessage] = useState("");

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ["settings-exchanges"] });
  }

  async function act(exchange: ExchangeId, action: "test" | "disable" | "delete") {
    if (action !== "test" && !window.confirm(`${action === "delete" ? "Delete" : "Disable"} ${LABELS[exchange]} credentials?`)) return;
    setBusy(exchange);
    setMessage("");
    try {
      if (action === "delete") await apiDelete(`/api/settings/exchanges/${exchange}`);
      else await apiPost(`/api/settings/exchanges/${exchange}/${action}`);
      setMessage(action === "test" ? "Read-only authentication check completed. No order was sent." : `Connection ${action}d and audited.`);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : `${action} failed.`);
    } finally {
      setBusy(null);
    }
  }

  async function save(exchange: ExchangeId, input: { api_key: string; api_secret: string; purpose: "read" | "trade"; withdrawal_disabled_ack: boolean }) {
    await apiPut<ExchangeConnectionPublic>(`/api/settings/exchanges/${exchange}`, input);
    setRotate(null);
    setMessage("Stored encrypted. Secret cannot be displayed.");
    await refresh();
  }

  return (
    <section className="rounded-xl border border-line bg-panel p-5">
      <div className="flex items-start justify-between gap-4">
        <div><h2 className="text-[15px] font-semibold">Exchange connections</h2><p className="mt-1 text-[11px] text-dim">Credential verification uses an authenticated read only call. It never sends an order.</p></div>
        <span className={`rounded border px-2 py-1 font-mono text-[10px] ${secretsReady ? "border-long/40 text-long" : "border-short/40 text-short"}`}>{secretsReady ? "ENCRYPTION READY" : "ENCRYPTION KEY MISSING"}</span>
      </div>
      {!secretsReady && <div className="mt-4 rounded-md border border-short/40 bg-short/5 px-3 py-2 text-[11px] text-short">Set VNEDGE_SECRETS_KEY on the server before storing credentials. No fallback key is generated.</div>}
      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        {(connections.data ?? []).map((item) => (
          <article key={item.exchange} className="rounded-lg border border-line bg-inset p-4">
            <div className="flex items-center justify-between gap-3"><h3 className="text-[13px] font-semibold">{LABELS[item.exchange]}</h3><span className={`rounded border px-2 py-1 font-mono text-[9px] uppercase ${tone[item.status]}`}>{item.status.replace("_", " ")}</span></div>
            <dl className="mt-4 space-y-2 text-[11px]">
              <div className="flex justify-between gap-3"><dt className="text-dim">Purpose</dt><dd className="font-mono uppercase">{item.purpose}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-dim">Key</dt><dd className="font-mono">{item.api_key_hint || "not stored"}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-dim">Last check</dt><dd className="font-mono text-right">{item.last_verified_at ? new Date(item.last_verified_at).toLocaleString() : "never"}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-dim">Private stream</dt><dd className="font-mono">{item.private_stream}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-dim">Live authority</dt><dd className="font-mono text-short">none</dd></div>
            </dl>
            {item.last_error && <div className="mt-3 text-[10px] text-short">{item.last_error}</div>}
            <div className="mt-4 flex flex-wrap gap-2">
              <button disabled={!secretsReady || busy === item.exchange} onClick={() => setRotate(item.exchange)} className="rounded border border-brand/40 px-3 py-1.5 text-[10px] text-brand disabled:opacity-40">{item.status === "not_configured" ? "Configure" : "Rotate"}</button>
              <button disabled={item.status === "not_configured" || item.status === "disabled" || busy === item.exchange} onClick={() => void act(item.exchange, "test")} className="rounded border border-line px-3 py-1.5 text-[10px] text-dim disabled:opacity-40">Test</button>
              <button disabled={item.status === "not_configured" || item.status === "disabled" || busy === item.exchange} onClick={() => void act(item.exchange, "disable")} className="rounded border border-warn/40 px-3 py-1.5 text-[10px] text-warn disabled:opacity-40">Disable</button>
              <button disabled={item.status === "not_configured" || busy === item.exchange} onClick={() => void act(item.exchange, "delete")} className="rounded border border-short/40 px-3 py-1.5 text-[10px] text-short disabled:opacity-40">Delete</button>
            </div>
          </article>
        ))}
      </div>
      {message && <div className="mt-4 text-[11px] text-dim" role="status">{message}</div>}
      {rotate && <RotateKeysModal exchange={rotate} label={LABELS[rotate]} onCancel={() => setRotate(null)} onSave={(input) => save(rotate, input)} />}
    </section>
  );
}
