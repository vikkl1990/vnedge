import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CommandPalette, type Command } from "./components/CommandPalette";
import { ResearchArena } from "./components/ResearchArena";
import { CockpitCommandBar } from "./components/CockpitCommandBar";
import { StrategyWorkbench } from "./components/StrategyWorkbench";
import { LiveStateBridge } from "./components/LiveStateBridge";
import { BuildVersionGuard } from "./components/BuildVersionGuard";
import { TerminalTabs } from "./components/Terminal";
import {
  Header,
  JournalPanel,
  DeskPanel,
  BookPanel,
  MarketPanel,
  PositionsPanel,
  RiskPanel,
  SystemPanel,
} from "./panels/Panels";
import { useUi } from "./store";
import { SettingsPanel } from "./panels/Settings/SettingsPanel";

const ScannerChart = lazy(() =>
  import("./components/ScannerChart").then((module) => ({
    default: module.ScannerChart,
  })),
);

const TABS = [
  { id: "strategy", label: "Strategy" },
  { id: "monitor", label: "Monitor" },
  { id: "tape", label: "Tape" },
  { id: "book", label: "Book" },
  { id: "evidence", label: "Evidence" },
  { id: "data", label: "Data" },
  { id: "arena", label: "Arena" },
  { id: "settings", label: "Settings" },
];

const LEGACY_TABS: Record<string, string> = {
  pulse: "strategy",
  patterns: "arena",
  chart: "tape",
  desk: "monitor",
  risk: "strategy",
  journal: "evidence",
  research: "arena",
  lab: "arena",
  system: "data",
};

export default function App() {
  const initialTab = () => {
    const raw = window.location.hash.replace(/^#\/?/, "").split("/", 1)[0];
    const candidate = LEGACY_TABS[raw] ?? raw;
    return TABS.some((item) => item.id === candidate) ? candidate : "strategy";
  };
  const [tab, setTab] = useState(initialTab);
  const [riskOpen, setRiskOpen] = useState(false);
  const riskDialogRef = useRef<HTMLDivElement | null>(null);
  const riskCloseRef = useRef<HTMLButtonElement | null>(null);
  const riskReturnFocusRef = useRef<HTMLElement | null>(null);
  const setPalette = useUi((s) => s.setPalette);
  const openRisk = useCallback(() => {
    riskReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setRiskOpen(true);
  }, []);
  const closeRisk = useCallback(() => {
    setRiskOpen(false);
    window.requestAnimationFrame(() => riskReturnFocusRef.current?.focus());
  }, []);
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
    if (!window.location.hash) window.history.replaceState(null, "", "#strategy");
    return () => {
      window.removeEventListener("hashchange", onHistory);
      window.removeEventListener("popstate", onHistory);
    };
  }, []);

  useEffect(() => {
    if (!riskOpen) return;
    riskCloseRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRisk();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        riskDialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [closeRisk, riskOpen]);

  const commands: Command[] = useMemo(
    () => [
      { id: "strategy", label: "Strategy", hint: "active system · chart · proof", run: () => navigate("strategy") },
      { id: "monitor", label: "Monitor", hint: "fleet drought · readiness · lanes", run: () => navigate("monitor") },
      { id: "tape", label: "Tape", hint: "canonical candles · evidence overlays", run: () => navigate("tape") },
      { id: "book", label: "Book", hint: "kernel book · positions · market", run: () => navigate("book") },
      { id: "evidence", label: "Evidence", hint: "decision identities · journal stream", run: () => navigate("evidence") },
      { id: "data", label: "Data", hint: "transport · lake · process health", run: () => navigate("data") },
      { id: "arena", label: "Arena", hint: "book · pipeline · backtests · agents", run: () => navigate("arena") },
      { id: "risk", label: "Open risk console", hint: "read-only halt · journal · checklist", run: openRisk },
      { id: "settings", label: "Settings", hint: "profile · encrypted exchange connections", run: () => navigate("settings") },
    ],
    [navigate, openRisk],
  );

  return (
    <div className="app-ambient min-h-full">
      <div className="app-ambient__orb app-ambient__orb--one" />
      <div className="app-ambient__orb app-ambient__orb--two" />
      <div className="app-shell mx-auto flex min-h-full max-w-[2200px] flex-col gap-3 px-3 py-3 md:px-5 md:py-4">
      <LiveStateBridge />
      <BuildVersionGuard />
      <Header />
      <CockpitCommandBar onOpenRisk={openRisk} />
      <div className="workbench-nav sticky top-0 z-30 flex items-center justify-between gap-3 px-2 py-2.5 backdrop-blur-xl flex-wrap">
        <TerminalTabs tabs={TABS} active={tab} onChange={navigate} />
        <button
          onClick={() => setPalette(true)}
          className="command-key"
        >
          <span>Command</span><kbd>⌘ K</kbd>
        </button>
      </div>

      {tab === "strategy" && <StrategyWorkbench />}
      {tab === "monitor" && <DeskPanel />}
      {tab === "tape" && (
        <Suspense
          fallback={
            <div className="rounded border border-line bg-panel px-4 py-12 text-center text-[11px] font-mono text-dim">
              Loading canonical chart renderer…
            </div>
          }
        >
          <ScannerChart />
        </Suspense>
      )}
      {tab === "book" && (
        <div className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-2"><BookPanel /><MarketPanel /></div>
          <PositionsPanel />
        </div>
      )}
      {tab === "evidence" && <JournalPanel />}
      {tab === "data" && <SystemPanel />}
      {tab === "arena" && <ResearchArena onNavigate={(next) => navigate(LEGACY_TABS[next] ?? next)} />}
      {tab === "settings" && <SettingsPanel />}

      <footer className="cockpit-footer">
        <span>VNEDGE / decision workstation</span>
        <span>measurement + shadow observation</span>
        <strong>no order, live-enable, or promotion controls</strong>
      </footer>

      <CommandPalette commands={commands} />
      {riskOpen && (
        <div ref={riskDialogRef} className="risk-drawer-backdrop" role="dialog" aria-modal="true" aria-label="Read-only risk console" onMouseDown={(event) => { if (event.target === event.currentTarget) closeRisk(); }}>
          <aside className="risk-drawer">
            <header><div><span className="eyebrow">Authority boundary</span><h2>Risk console</h2></div><button ref={riskCloseRef} type="button" onClick={closeRisk} aria-label="Close risk console">×</button></header>
            <div className="risk-drawer__body"><RiskPanel /></div>
            <footer>Read only · reduce-only safeguards remain server-owned</footer>
          </aside>
        </div>
      )}
      </div>
    </div>
  );
}
