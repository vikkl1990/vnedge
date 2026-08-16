import { useMemo, useState } from "react";
import { CommandPalette, type Command } from "./components/CommandPalette";
import { MarketPulse } from "./components/MarketPulse";
import { TerminalTabs } from "./components/Terminal";
import {
  Header,
  JournalPanel,
  LanesPanel,
  LiveBlockedBanner,
  ResearchPanel,
  RiskPanel,
} from "./panels/Panels";
import { useUi } from "./store";

const TABS = [
  { id: "pulse", label: "Pulse" },
  { id: "lanes", label: "Lanes" },
  { id: "risk", label: "Risk" },
  { id: "journal", label: "Journal" },
  { id: "research", label: "Research" },
];

export default function App() {
  const [tab, setTab] = useState("pulse");
  const setPalette = useUi((s) => s.setPalette);

  const commands: Command[] = useMemo(
    () => [
      { id: "pulse", label: "Pulse", hint: "1h story · VWAP · AI observation", run: () => setTab("pulse") },
      { id: "lanes", label: "Lanes", hint: "eligibility · mode · capital", run: () => setTab("lanes") },
      { id: "risk", label: "Risk", hint: "kill · journal · streams", run: () => setTab("risk") },
      { id: "journal", label: "Journal", hint: "read-only decisions", run: () => setTab("journal") },
      { id: "research", label: "Research", hint: "evidence only", run: () => setTab("research") },
      {
        id: "classic",
        label: "Classic dashboard ↗",
        hint: "the full v1 cockpit",
        run: () => {
          const t = new URLSearchParams(window.location.search).get("token");
          window.location.href = t ? `/?token=${encodeURIComponent(t)}` : "/";
        },
      },
    ],
    [setTab],
  );

  return (
    <div className="min-h-full max-w-[1180px] mx-auto px-5 py-6 flex flex-col gap-5">
      <Header />
      <LiveBlockedBanner />
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
      {tab === "lanes" && <LanesPanel />}
      {tab === "risk" && <RiskPanel />}
      {tab === "journal" && <JournalPanel />}
      {tab === "research" && <ResearchPanel />}

      <footer className="text-[11px] font-mono text-faint pt-2">
        correction cockpit · read-only · no order or promotion controls
      </footer>

      <CommandPalette commands={commands} />
    </div>
  );
}
