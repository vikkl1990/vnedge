import { apiPost } from "../../api";
import { useLanes, useRiskSnapshot, useSettingsSecurity, useWhoAmI } from "../../queries";
import { ExchangesPanel } from "./ExchangesPanel";
import { ProfilePanel } from "./ProfilePanel";

export function SettingsPanel() {
  const who = useWhoAmI();
  const security = useSettingsSecurity();
  const lanes = useLanes();
  const risk = useRiskSnapshot();
  const allowed = who.data?.permissions.includes("manage_settings");

  if (who.isLoading) return <div className="text-[12px] font-mono text-dim">loading operator permissions…</div>;
  if (!allowed) return <div className="rounded-xl border border-short/40 bg-short/5 p-5 text-[12px] text-short">Operator permission is required for Settings.</div>;

  async function rotateSession() {
    await apiPost("/api/settings/session/rotate");
    window.location.reload();
  }

  return (
    <div className="space-y-4">
      <section className="rounded-xl border border-line bg-panel p-5">
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div><h1 className="text-[20px] font-semibold">Settings</h1><p className="mt-1 text-[11px] text-dim">Profile and encrypted connections. No live-enable, order, or promotion controls exist here.</p></div>
          <div className="flex items-center gap-2"><span className="rounded border border-long/40 px-2 py-1 font-mono text-[10px] text-long">HTTPONLY SESSION</span><button onClick={() => void rotateSession()} className="rounded border border-line px-3 py-1.5 text-[10px] text-dim">Rotate session</button></div>
        </div>
      </section>
      <ProfilePanel />
      <section className="rounded-xl border border-line bg-panel p-5">
        <div className="font-mono text-[10px] uppercase tracking-wider text-faint">Effective read-only operating contract</div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div className="rounded-lg border border-line bg-inset p-3"><div className="text-[10px] text-faint">SHADOW PURSE</div><div className="mt-1 font-mono text-lg">${(lanes.data?.portfolio.shadow_purse_usd ?? 0).toFixed(2)}</div></div>
          <div className="rounded-lg border border-line bg-inset p-3"><div className="text-[10px] text-faint">CAPITAL ROSTER</div><div className="mt-1 font-mono text-lg">{risk.data?.capital.roster_size ?? 0} · OFF</div></div>
          <div className="rounded-lg border border-line bg-inset p-3"><div className="text-[10px] text-faint">SCANNER LANES</div><div className="mt-1 font-mono text-lg">{lanes.data?.shadow_observe_lanes ?? 0} virtual</div><div className="mt-1 font-mono text-[9px] text-faint">{lanes.data?.shadow_observe_timeframes?.join(" + ") || "not configured"} · roster {lanes.data?.lane_set_hash?.slice(0, 8) || "—"}</div></div>
          <div className="rounded-lg border border-line bg-inset p-3"><div className="text-[10px] text-faint">SESSION EXPIRES</div><div className="mt-1 font-mono text-[11px]">{who.data?.expires_at ? new Date(who.data.expires_at).toLocaleString() : "not reported"}</div></div>
        </div>
      </section>
      <ExchangesPanel secretsReady={security.data?.secrets_store_ready ?? false} />
      <section className="rounded-xl border border-short/30 bg-short/5 p-4 text-[11px] text-dim"><strong className="text-short">Live remains blocked independently.</strong> A verified trade credential still requires the runtime flags, checklist, kill/journal health, promotion ladder, and required private stream.</section>
    </div>
  );
}
