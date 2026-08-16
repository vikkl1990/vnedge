import { useState } from "react";
import type { ExchangeId, KeyPurpose } from "../../api";

interface Props {
  exchange: ExchangeId;
  label: string;
  onCancel: () => void;
  onSave: (input: { api_key: string; api_secret: string; purpose: KeyPurpose; withdrawal_disabled_ack: boolean }) => Promise<void>;
}

export function RotateKeysModal({ exchange, label, onCancel, onSave }: Props) {
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [purpose, setPurpose] = useState<KeyPurpose>("trade");
  const [ack, setAck] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const valid = apiKey.trim() && apiSecret.trim() && (purpose === "read" || ack);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid) return;
    setSaving(true);
    setError("");
    try {
      await onSave({
        api_key: apiKey,
        api_secret: apiSecret,
        purpose,
        withdrawal_disabled_ack: purpose === "read" || ack,
      });
      setApiKey("");
      setApiSecret("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Credential save failed.");
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4" role="dialog" aria-modal="true" aria-labelledby="rotate-title">
      <form onSubmit={submit} autoComplete="off" className="w-full max-w-lg rounded-xl border border-line2 bg-panel p-5 shadow-2xl">
        <h2 id="rotate-title" className="text-[16px] font-semibold">Store {label} credentials</h2>
        <p className="mt-1 text-[11px] text-dim">Encrypted on the server. The secret cannot be displayed after save.</p>
        <input type="hidden" value={exchange} />
        <div className="mt-5 space-y-4">
          <label className="block text-[11px] text-dim">API key
            <input autoFocus required autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} className="mt-1 w-full rounded-md border border-line bg-inset px-3 py-2 font-mono text-txt" />
          </label>
          <label className="block text-[11px] text-dim">API secret
            <input type="password" required autoComplete="new-password" value={apiSecret} onChange={(event) => setApiSecret(event.target.value)} className="mt-1 w-full rounded-md border border-line bg-inset px-3 py-2 font-mono text-txt" />
          </label>
          <fieldset className="flex gap-5 text-[11px] text-dim">
            <legend className="mb-2">Purpose</legend>
            <label className="flex gap-2"><input type="radio" checked={purpose === "read"} onChange={() => setPurpose("read")} /> Read-only</label>
            <label className="flex gap-2"><input type="radio" checked={purpose === "trade"} onChange={() => setPurpose("trade")} /> Trade</label>
          </fieldset>
          {purpose === "trade" && <label className="flex gap-2 text-[11px] text-warn"><input type="checkbox" checked={ack} onChange={(event) => setAck(event.target.checked)} /> I created trade-only keys with withdrawals disabled.</label>}
          {error && <div className="rounded-md border border-short/40 bg-short/5 px-3 py-2 text-[11px] text-short" role="alert">{error}</div>}
        </div>
        <div className="mt-5 flex justify-end gap-3">
          <button type="button" onClick={onCancel} className="rounded-md border border-line px-4 py-2 text-[11px] text-dim">Cancel</button>
          <button disabled={!valid || saving} className="rounded-md border border-brand/60 bg-brand/10 px-4 py-2 text-[11px] text-brand disabled:opacity-40">{saving ? "Encrypting…" : "Save — show never again"}</button>
        </div>
      </form>
    </div>
  );
}
