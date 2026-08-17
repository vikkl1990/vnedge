import { useCallback, useEffect, useMemo, useState } from "react";
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
  const initialTab = () => {
    const candidate = window.location.hash.replace(/^#\/?/, "");
    return TABS.some((item) => item.id === candidate) ? candidate : "pulse";
  };
  const [tab, setTab] = useState(initialTab);
  const setPalette = useUi((s) => s.setPalette);
  const navigate = useCallback((next: string) => {
    if (!TABS.some((item) => item.id === next)) return;
    setTab(next);
    if (window.location.hash !== `#${next}`) window.history.pushState(null, "", `#${next}`);
    window.scrollTo({ top: 0, behavior: "instant" });
  }, []);

  useEffect(() => {
    const onHistory = () => {
      setTab(initialTab());
      window.scrollTo({ top: 0, behavior: "instant" });
    };
    window.addEventListener("hashchange", onHistory);
    window.addEventListener("popstate", onHistory);
    if (!window.location.hash) window.history.replaceState(null, "", "#pulse");
    return () => {
      window.removeEventListener("hashchange", onHistory);
      window.removeEventListener("popstate", onHistory);
    };
  }, []);

  const commands: Command[] = useMemo(
    () => [
      { id: "pulse", label: "Pulse", hint: "1h story · VWAP · scanner observer", run: () => navigate("pulse") },
      { id: "desk", label: "Desk", hint: "runtime lanes · sizing · virtual outcomes", run: () => navigate("desk") },
      { id: "risk", label: "Risk", hint: "purse · margin · leverage · gates", run: () => navigate("risk") },
      { id: "journal", label: "Journal", hint: "decisions · signals · outcomes", run: () => navigate("journal") },
      { id: "research", label: "Research", hint: "after-cost evidence · ML · agents", run: () => navigate("research") },
      { id: "promote", label: "Promote", hint: "human gates · sealed strategies", run: () => navigate("promote") },
      { id: "system", label: "System", hint: "freshness · resources · transport", run: () => navigate("system") },
      { id: "settings", label: "Settings", hint: "profile · encrypted exchange connections", run: () => navigate("settings") },
      {
        id: "classic",
        label: "Legacy dashboard ↗",
        hint: "explicit fallback",
        run: () => {
          window.location.href = "/";
        },
      },
    ],
    [navigate],
  );

  return (
    <div className="min-h-full max-w-[1500px] mx-auto px-5 py-6 flex flex-col gap-5">
      <Header />
      <LiveBlockedBanner />
      <StatusStrip />
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <TerminalTabs tabs={TABS} active={tab} onChange={navigate} />
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
