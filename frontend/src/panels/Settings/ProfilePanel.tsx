import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiPut, type OperatorProfile } from "../../api";
import { useOperatorProfile } from "../../queries";

export function ProfilePanel() {
  const profile = useOperatorProfile();
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("");
  const [timezone, setTimezone] = useState("UTC");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!profile.data) return;
    setDisplayName(profile.data.display_name);
    setTimezone(profile.data.timezone);
  }, [profile.data]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setMessage("");
    try {
      const updated = await apiPut<OperatorProfile>("/api/settings/profile", {
        display_name: displayName,
        timezone,
      });
      queryClient.setQueryData(["settings-profile"], updated);
      setMessage("Profile saved and audited.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Profile save failed.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="rounded-xl border border-line bg-panel p-5">
      <div className="mb-4">
        <h2 className="text-[15px] font-semibold">Profile</h2>
        <p className="mt-1 text-[11px] text-dim">Identity and display preferences only.</p>
      </div>
      <form onSubmit={save} className="grid gap-4 md:grid-cols-3" autoComplete="off">
        <label className="text-[11px] text-dim">
          Operator
          <input readOnly value={profile.data?.operator_id ?? "…"} className="mt-1 w-full rounded-md border border-line bg-inset px-3 py-2 font-mono text-txt opacity-70" />
        </label>
        <label className="text-[11px] text-dim">
          Display name
          <input required maxLength={80} value={displayName} onChange={(event) => setDisplayName(event.target.value)} className="mt-1 w-full rounded-md border border-line bg-inset px-3 py-2 text-txt" />
        </label>
        <label className="text-[11px] text-dim">
          Timezone
          <select value={timezone} onChange={(event) => setTimezone(event.target.value)} className="mt-1 w-full rounded-md border border-line bg-inset px-3 py-2 text-txt">
            <option value="UTC">UTC</option>
            <option value="Asia/Kolkata">Asia/Kolkata</option>
            <option value="America/New_York">America/New_York</option>
            <option value="Europe/London">Europe/London</option>
            <option value="Asia/Singapore">Asia/Singapore</option>
          </select>
        </label>
        <div className="md:col-span-3 flex items-center gap-3">
          <button disabled={saving || !displayName.trim()} className="rounded-md border border-brand/60 bg-brand/10 px-4 py-2 text-[11px] text-brand disabled:opacity-40">{saving ? "Saving…" : "Save profile"}</button>
          {message && <span className="text-[11px] text-dim" role="status">{message}</span>}
        </div>
      </form>
    </section>
  );
}
