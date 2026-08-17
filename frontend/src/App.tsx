import { useMemo, useState } from "react";
import { CommandPalette, type Command } from "./components/CommandPalette";
import { MarketPulse } from "./components/MarketPulse";
import { TerminalTabs } from "./components/Terminal";
import {
  Header,
  JournalPanel,
  DeskPanel,
  BookPanel,
  LiveBlockedBanner,
  MarketPanel,
  PositionsPanel,
  PromotePanel,
  ResearchPanel,
  RiskPanel,
  StatusStrip,
  SystemPanel,
} from "./panels/Panels";
import { useUi } from "./store";
import { SettingsPanel } from "./panels/Settings/SettingsPanel";

const TABS = [
  { id: "pulse", label: "Pulse" },
  { id: "desk", label: "Desk" },
  { id: "risk", label: "Risk" },
  { id: "journal", label: "Journal" },
  { id: "research", label: "Research" },
  { id: "promote", label: "Promote" },
  { id: "system", label: "System" },
  { id: "settings", label: "Settings" },
];

export default function App() {
  const [tab, setTab] = useState("pulse");
  const setPalette = useUi((s) => s.setPalette);

  const commands: Command[] = useMemo(
    () => [
      { id: "pulse", label: "Pulse", hint: "1h story · VWAP · AI observation", run: () => setTab("pulse") },
      { id: "desk", label: "Desk", hint: "runtime lanes · eligibility · capital", run: () => setTab("desk") },
      { id: "risk", label: "Risk", hint: "kill · journal · streams", run: () => setTab("risk") },
      { id: "journal", label: "Journal", hint: "read-only decisions", run: () => setTab("journal") },
      { id: "research", label: "Research", hint: "evidence · ML · agents", run: () => setTab("research") },
      { id: "promote", label: "Promote", hint: "human gates · sealed strategies", run: () => setTab("promote") },
      { id: "system", label: "System", hint: "freshness · health · build", run: () => setTab("system") },
      { id: "settings", label: "Settings", hint: "profile · encrypted exchange connections", run: () => setTab("settings") },
      {
        id: "classic",
        label: "Legacy dashboard ↗",
        hint: "explicit fallback",
        run: () => {
          window.location.href = "/";
        },
      },
    ],
    [setTab],
  );

  return (
    <div className="min-h-full max-w-[1500px] mx-auto px-5 py-6 flex flex-col gap-5">
      <Header />
      <LiveBlockedBanner />
      <StatusStrip />
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <TerminalTabs tabs={TABS} active={tab} onChange={setTab} />
        <button
          onClick={() => setPalette(true)}
          className="rounded-md border border-line px-3 py-1.5 text-[12px] font-mono text-dim hover:text-txt"
        >
          ⌘/Ctrl-K
        </button>
      </div>

      {tab === "pulse" && <MarketPulse />}
      {tab === "desk" && (
        <div className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-2"><BookPanel /><MarketPanel /></div>
          <DeskPanel />
          <PositionsPanel />
        </div>
      )}
      {tab === "risk" && <RiskPanel />}
      {tab === "journal" && <JournalPanel />}
      {tab === "research" && <ResearchPanel />}
      {tab === "promote" && <PromotePanel />}
      {tab === "system" && <SystemPanel />}
      {tab === "settings" && <SettingsPanel />}

      <footer className="text-[11px] font-mono text-faint pt-2">
        correction cockpit · scoped settings only · no order, live-enable, or promotion controls
      </footer>

      <CommandPalette commands={commands} />
    </div>
  );
}
