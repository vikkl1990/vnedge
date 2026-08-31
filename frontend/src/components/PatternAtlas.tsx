import { useMemo, useState } from "react";
import type { CorrectionLane, ResearchScorecard } from "../api";
import { useLanes, useResearchScorecard } from "../queries";
import { DenseTable, TerminalBadge, TerminalPanel, type Column } from "./Terminal";

type PatternFamily = "expansion" | "continuation" | "reclaim" | "reversal";
type EvidenceTone = "neutral" | "good" | "warn" | "bad" | "info";

interface PatternDefinition {
  id: string;
  name: string;
  thesis: string;
  family: PatternFamily;
  decisionTf: "5m" | "15m" | "1h";
  context: string;
  entryClock: string;
  protectionClock: string;
  regime: string;
  direction: "two-sided" | "with HTF" | "counter-move";
  rules: string[];
  invalidation: string;
  economics: string;
  caution: string;
  strategyIds: string[];
  sketch: "squeeze" | "range" | "bos" | "reclaim" | "session" | "sweep" | "pullback" | "regime";
}

const PATTERNS: PatternDefinition[] = [
  {
    id: "squeeze-expansion",
    name: "Compression → Expansion",
    thesis: "A quiet 5m box releases with enough volume and executable quote acceptance.",
    family: "expansion",
    decisionTf: "5m",
    context: "closed 5m box",
    entryClock: "BBO hold after close",
    protectionClock: "ticks · 30–60m horizon",
    regime: "range or trend pullback",
    direction: "two-sided",
    rules: ["Compression is present before the break", "Closed bar clears the frozen box", "BBO holds beyond the level; spread and chase remain valid"],
    invalidation: "Back inside the box, stale/overflowed book, or the opposite box edge.",
    economics: "Delta scalp tariff. A small gross move is not a setup that survived costs.",
    caution: "Fastest family and the most fee-sensitive. Current evidence has been weak after booked costs.",
    strategyIds: ["squeeze_expansion_breakout_v3", "squeeze_expansion_breakout_v4", "tick_accepted_breakout_v1"],
    sketch: "squeeze",
  },
  {
    id: "range-expansion",
    name: "Range Expansion",
    thesis: "A prior 15m balance resolves beyond its boundary without using the forming candle.",
    family: "expansion",
    decisionTf: "15m",
    context: "prior closed range",
    entryClock: "next open or BBO hold",
    protectionClock: "ticks · 12h backstop",
    regime: "range ending or expansion beginning",
    direction: "two-sided",
    rules: ["A measurable range exists before the decision bar", "Expansion clears the prior boundary", "Room after costs remains before the next structure wall"],
    invalidation: "Return through the broken boundary or failed acceptance.",
    economics: "Delta swing profile; gate reserve is not booked P&L.",
    caution: "A large candle that already travelled through the target is not a fresh entry.",
    strategyIds: ["range_expansion_observer_v3", "range_expansion_observer_v4", "range_expansion_realtime_v1", "range_expansion_realtime_v2"],
    sketch: "range",
  },
  {
    id: "structure-bos",
    name: "Break of Structure",
    thesis: "Confirmed swings establish structure; a closed 15m break must agree with last-closed HTF context.",
    family: "continuation",
    decisionTf: "15m",
    context: "confirmed swings · closed 4h",
    entryClock: "next open or BBO hold",
    protectionClock: "ticks · structure invalidation",
    regime: "continuation only",
    direction: "with HTF",
    rules: ["Swing anchors are confirmed causally", "Break clears the latest structure with its frozen buffer", "4h context does not oppose the side"],
    invalidation: "Opposite confirmed swing or HTF bias invalidation.",
    economics: "Swing tariff; stops are snapped to the Delta tick grid before sizing.",
    caution: "Fractal confirmation is intentionally selective. More fires is not automatically better structure.",
    strategyIds: ["structure_bos_1h", "structure_bos_15m_trigger_v2", "structure_bos_15m_trigger_v3", "structure_bos_realtime_v1", "structure_bos_realtime_v2"],
    sketch: "bos",
  },
  {
    id: "htf-regime-continuation",
    name: "HTF Regime Continuation",
    thesis: "Weekly/daily/4h permission chooses the playbook; a 15m reclaim supplies the entry geometry.",
    family: "continuation",
    decisionTf: "15m",
    context: "closed 1w · 1d · 4h",
    entryClock: "next 15m open",
    protectionClock: "ticks · flatten on HTF invalidation",
    regime: "continuation with one allowed side",
    direction: "with HTF",
    rules: ["Last-closed weekly structure resolves up or down", "Daily EMA/MACD impulse is not fading at an extreme", "4h agrees and 15m reclaims in the permitted direction"],
    invalidation: "Closed 4h flips against the weekly side or the 15m reclaim fails.",
    economics: "Delta swing profile; funding is applied only if a held position crosses a real print.",
    caution: "V1 requires complete trade-lake weekly VWAP. V2 uses OHLC range/structure and is a separate frozen hypothesis.",
    strategyIds: ["htf_regime_continuation_15m_v1", "htf_regime_continuation_15m_v2", "htf_structure_continuation_realtime_v1"],
    sketch: "regime",
  },
  {
    id: "avwap-reclaim",
    name: "Anchored VWAP Reclaim",
    thesis: "Price regains an event-anchored cost basis after a causal swing anchor is confirmed.",
    family: "reclaim",
    decisionTf: "15m",
    context: "dual AVWAP · confirmed swings",
    entryClock: "next 15m open",
    protectionClock: "ticks · anchor failure",
    regime: "pullback inside aligned structure",
    direction: "with HTF",
    rules: ["Both anchors are causal and available", "Close reclaims the relevant AVWAP", "The opposite AVWAP does not create a strong conflict"],
    invalidation: "Loss of the reclaimed AVWAP plus structure failure.",
    economics: "Swing tariff; AVWAP is context, never a fee bypass.",
    caution: "A reclaim is location, not proof of expectancy. Thin samples remain under-sampled.",
    strategyIds: ["avwap_reclaim_15m_v1"],
    sketch: "reclaim",
  },
  {
    id: "session-continuation",
    name: "Session Continuation",
    thesis: "An active UTC block extends an already-aligned move after range and volume wake up.",
    family: "continuation",
    decisionTf: "15m",
    context: "session clock · 4h bias",
    entryClock: "next open or BBO hold",
    protectionClock: "ticks · session/structure exit",
    regime: "continuation during eligible hours",
    direction: "with HTF",
    rules: ["Evaluation is inside the frozen session", "Range/volume expansion clears its hour-of-day baseline", "The side agrees with higher-timeframe permission"],
    invalidation: "Session drive fails back through its origin or HTF permission disappears.",
    economics: "Swing tariff. The busy hour is not itself directional edge.",
    caution: "Outside-session rows are correct no-trades, not a dead scanner.",
    strategyIds: ["session_continuation_15m_v1", "session_continuation_realtime_v1", "session_continuation_realtime_v2"],
    sketch: "session",
  },
  {
    id: "liquidity-sweep",
    name: "Liquidity Sweep Reversal",
    thesis: "A closed 15m bar trades beyond a prior extreme and rejects back through it.",
    family: "reversal",
    decisionTf: "15m",
    context: "prior swing extreme",
    entryClock: "next 15m open",
    protectionClock: "ticks · sweep extreme",
    regime: "mean reversion only",
    direction: "counter-move",
    rules: ["A real prior liquidity extreme exists", "The decision bar sweeps and closes back inside", "Counter-trend permission is explicit; continuation regime blocks the fade"],
    invalidation: "Price accepts beyond the swept extreme.",
    economics: "Swing tariff. Gross-negative evidence cannot be repaired with optimistic fees.",
    caution: "This family has produced negative local replay evidence and must remain research-only.",
    strategyIds: ["liquidity_sweep_reversal_15m_v1"],
    sketch: "sweep",
  },
  {
    id: "trend-pullback",
    name: "Trend Pullback",
    thesis: "A 1h pullback preserves the larger trend and resumes without chasing the impulse bar.",
    family: "reclaim",
    decisionTf: "1h",
    context: "closed 4h/daily direction",
    entryClock: "next 1h open",
    protectionClock: "ticks · 48h backstop",
    regime: "continuation after pullback",
    direction: "with HTF",
    rules: ["Higher-timeframe trend remains intact", "Pullback reaches a defined value/structure zone", "Closed 1h bar resumes in the permitted direction"],
    invalidation: "Pullback becomes an HTF structure break.",
    economics: "Swing tariff; larger horizon is intended to amortize fixed round-trip cost.",
    caution: "One positive trade is a case study, not a scorecard.",
    strategyIds: ["trend_pullback_1h_v1", "trend_squeeze_continuation_1h_v1"],
    sketch: "pullback",
  },
];

function PatternSketch({ kind }: { kind: PatternDefinition["sketch"] }) {
  const paths: Record<PatternDefinition["sketch"], string> = {
    squeeze: "M8 50 L24 48 L38 51 L54 47 L72 49 L88 46 L104 48 L120 31 L136 18 L152 12",
    range: "M8 48 L22 29 L38 45 L54 27 L70 44 L86 30 L102 43 L118 25 L134 15 L152 8",
    bos: "M8 55 L28 39 L44 48 L64 28 L82 39 L102 20 L120 31 L138 12 L152 16",
    reclaim: "M8 18 L26 27 L44 39 L62 51 L80 47 L98 35 L116 41 L134 25 L152 18",
    session: "M8 50 L28 47 L48 49 L66 45 L82 30 L98 17 L116 25 L134 11 L152 15",
    sweep: "M8 30 L28 27 L48 31 L66 26 L84 58 L98 29 L118 22 L138 30 L152 20",
    pullback: "M8 53 L26 38 L44 22 L62 30 L78 40 L96 31 L114 18 L132 25 L152 10",
    regime: "M8 56 L28 48 L46 52 L64 40 L82 33 L100 36 L118 22 L136 15 L152 9",
  };
  return (
    <svg viewBox="0 0 160 66" className="h-[78px] w-full" role="img" aria-label="Illustrative pattern anatomy, not market data">
      <path d="M4 54 H156 M4 34 H156 M4 14 H156" stroke="currentColor" className="text-line" strokeWidth="1" strokeDasharray="2 5" />
      <path d={paths[kind]} fill="none" stroke="currentColor" className="text-brand" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx="134" cy={kind === "sweep" ? "30" : kind === "reclaim" ? "25" : "15"} r="3" fill="currentColor" className="text-warn" />
    </svg>
  );
}

function evidenceFor(pattern: PatternDefinition, scorecard?: ResearchScorecard) {
  const rows = scorecard?.strategies.filter((row) => pattern.strategyIds.includes(row.strategy)) ?? [];
  if (!rows.length) return { label: "UNSCORED", tone: "neutral" as EvidenceTone, detail: "no exact-ID scorecard" };
  const qualified = rows.filter((row) => row.sample_qualified);
  const best = [...rows].sort((a, b) => (b.best_net_bps ?? -Infinity) - (a.best_net_bps ?? -Infinity))[0];
  const verdict = String(best.verdict ?? best.source_verdict ?? "not reported").toUpperCase();
  const failed = /FAIL|REJECT|KILL|NEGATIVE/.test(verdict);
  return {
    label: qualified.length ? verdict : "UNDER_SAMPLED",
    tone: (failed ? "bad" : qualified.length ? "good" : "warn") as EvidenceTone,
    detail: `${best.samples ?? 0} ${best.sample_unit || "samples"} · ${best.best_net_bps == null ? "net unreported" : `${best.best_net_bps >= 0 ? "+" : ""}${best.best_net_bps.toFixed(1)} bps`}`,
  };
}

function lanesFor(pattern: PatternDefinition, lanes: CorrectionLane[]) {
  return lanes.filter((lane) => pattern.strategyIds.includes(lane.strategy_id));
}

function runtimeState(pattern: PatternDefinition, lanes: CorrectionLane[]) {
  const rows = lanesFor(pattern, lanes);
  const priority = ["accepted", "holding", "armed", "watching", "session_blocked", "degraded"];
  const state = priority.find((candidate) => rows.some((row) => row.lifecycle.state === candidate)) ?? "not rostered";
  return {
    rows,
    state,
    armed: rows.reduce((sum, row) => sum + row.lifecycle.armed_entries, 0),
    candidates: rows.reduce((sum, row) => sum + row.lifecycle.candidates, 0),
    accepted: rows.reduce((sum, row) => sum + row.lifecycle.accepted, 0),
    resolved: rows.reduce((sum, row) => sum + row.lifecycle.resolved, 0),
    netUsd: rows.reduce((sum, row) => sum + (row.lifecycle.net_unit === "USD" ? row.lifecycle.net_value ?? 0 : 0), 0),
    blocked: rows.filter((row) => row.health === "blocked").length,
    waiting: [...new Set(rows.map((row) => row.current_waiting_reason).filter(Boolean))].slice(0, 3),
  };
}

function stateTone(state: string): EvidenceTone {
  if (state === "accepted" || state === "holding") return "good";
  if (state === "armed") return "info";
  if (state === "degraded") return "bad";
  if (state === "session_blocked" || state === "not rostered") return "neutral";
  return "warn";
}

function signedUsd(value: number) {
  return `${value < 0 ? "−" : value > 0 ? "+" : ""}$${Math.abs(value).toFixed(2)}`;
}

export function PatternAtlas() {
  const lanesQuery = useLanes();
  const scorecardQuery = useResearchScorecard();
  const lanes = lanesQuery.data?.lanes ?? [];
  const [selectedId, setSelectedId] = useState(PATTERNS[0].id);
  const [family, setFamily] = useState<"all" | PatternFamily>("all");
  const [tf, setTf] = useState<"all" | PatternDefinition["decisionTf"]>("all");
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => PATTERNS.filter((pattern) => {
    const haystack = `${pattern.name} ${pattern.thesis} ${pattern.family} ${pattern.regime} ${pattern.strategyIds.join(" ")}`.toLowerCase();
    return (family === "all" || pattern.family === family)
      && (tf === "all" || pattern.decisionTf === tf)
      && (!query.trim() || haystack.includes(query.trim().toLowerCase()));
  }), [family, query, tf]);
  const selected = PATTERNS.find((pattern) => pattern.id === selectedId) ?? filtered[0] ?? PATTERNS[0];
  const selectedRuntime = runtimeState(selected, lanes);
  const activeSetups = PATTERNS.filter((pattern) => ["armed", "accepted", "holding"].includes(runtimeState(pattern, lanes).state)).length;
  const totalAccepted = PATTERNS.reduce((sum, pattern) => sum + runtimeState(pattern, lanes).accepted, 0);
  const totalBlocked = lanes.filter((lane) => lane.health === "blocked").length;
  const recordRows = PATTERNS.map((pattern) => ({ pattern, runtime: runtimeState(pattern, lanes), evidence: evidenceFor(pattern, scorecardQuery.data) }));
  const recordColumns: Column<(typeof recordRows)[number]>[] = [
    { key: "pattern", header: "Pattern", render: (row) => <button className="text-left" onClick={() => setSelectedId(row.pattern.id)}><span className="font-semibold text-txt">{row.pattern.name}</span><span className="block text-[9px] font-mono text-faint">{row.pattern.decisionTf} · {row.pattern.family}</span></button> },
    { key: "state", header: "Current state", render: (row) => <TerminalBadge tone={stateTone(row.runtime.state)}>{row.runtime.state}</TerminalBadge> },
    { key: "funnel", header: "Observed funnel", render: (row) => <span className="font-mono">{row.runtime.armed} → {row.runtime.candidates} → {row.runtime.accepted}</span> },
    { key: "resolved", header: "Resolved", align: "right", render: (row) => <span className="font-mono">{row.runtime.resolved}</span> },
    { key: "net", header: "Shadow booked", align: "right", render: (row) => <span className={`font-mono ${row.runtime.netUsd < 0 ? "text-short" : row.runtime.netUsd > 0 ? "text-long" : "text-dim"}`}>{signedUsd(row.runtime.netUsd)}</span> },
    { key: "evidence", header: "Evidence", render: (row) => <span><TerminalBadge tone={row.evidence.tone}>{row.evidence.label}</TerminalBadge><span className="mt-1 block text-[9px] text-faint">{row.evidence.detail}</span></span> },
    { key: "why", header: "Why not now", render: (row) => <span className="text-dim">{row.runtime.waiting.join(" · ") || (row.runtime.rows.length ? "no active setup" : "not on runtime roster")}</span> },
  ];

  return (
    <div className="space-y-4">
      <section className="overflow-hidden rounded-md border border-line bg-panel/70">
        <div className="grid gap-0 lg:grid-cols-[1.25fr_.75fr]">
          <div className="p-5 md:p-7">
            <div className="font-mono text-[10px] font-bold uppercase tracking-[.2em] text-brand">VNEDGE Pattern Atlas</div>
            <h1 className="mt-3 max-w-3xl text-2xl font-semibold leading-tight text-txt md:text-4xl">Watch leverage reveal structure.</h1>
            <p className="mt-3 max-w-3xl text-[13px] leading-6 text-dim">Published crypto/perpetual setup definitions, applied on causal clocks and shown with their failures. Patterns describe structure; the fee wall, risk gateway, and evidence ladder decide whether anything survives.</p>
            <div className="mt-5 flex flex-wrap gap-2">
              <TerminalBadge tone="info">rules, not opinions</TerminalBadge>
              <TerminalBadge tone="warn">probabilities, not predictions</TerminalBadge>
              <TerminalBadge tone="neutral">read only</TerminalBadge>
              <TerminalBadge tone="bad">capital locked</TerminalBadge>
            </div>
          </div>
          <div className="grid grid-cols-3 border-t border-line bg-inset/60 lg:border-l lg:border-t-0">
            <div className="flex flex-col justify-center border-r border-line p-4"><span className="font-mono text-[10px] uppercase text-faint">Patterns</span><strong className="mt-2 font-mono text-3xl">{PATTERNS.length}</strong><span className="mt-1 text-[10px] text-dim">published anatomy</span></div>
            <div className="flex flex-col justify-center border-r border-line p-4"><span className="font-mono text-[10px] uppercase text-faint">Active now</span><strong className="mt-2 font-mono text-3xl text-info">{activeSetups}</strong><span className="mt-1 text-[10px] text-dim">armed / accepted</span></div>
            <div className="flex flex-col justify-center p-4"><span className="font-mono text-[10px] uppercase text-faint">Accepted</span><strong className="mt-2 font-mono text-3xl">{totalAccepted}</strong><span className="mt-1 text-[10px] text-dim">runtime cumulative</span></div>
          </div>
        </div>
      </section>

      <TerminalPanel title="Pattern screens" meta={`${filtered.length}/${PATTERNS.length} shown · ${totalBlocked} runtime lanes blocked`}>
        <div className="mb-4 flex flex-wrap gap-2">
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="search pattern, regime, scanner…" className="min-h-9 min-w-[250px] flex-1 rounded-md border border-line bg-bg px-3 text-[11px] font-mono text-txt placeholder:text-faint" />
          <select value={family} onChange={(event) => setFamily(event.target.value as typeof family)} className="min-h-9 rounded-md border border-line bg-bg px-3 text-[11px] font-mono"><option value="all">all families</option><option value="expansion">expansion</option><option value="continuation">continuation</option><option value="reclaim">reclaim</option><option value="reversal">reversal</option></select>
          <select value={tf} onChange={(event) => setTf(event.target.value as typeof tf)} className="min-h-9 rounded-md border border-line bg-bg px-3 text-[11px] font-mono"><option value="all">all clocks</option><option value="5m">5m</option><option value="15m">15m</option><option value="1h">1h</option></select>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {filtered.map((pattern) => {
            const runtime = runtimeState(pattern, lanes);
            const evidence = evidenceFor(pattern, scorecardQuery.data);
            const selectedCard = selected.id === pattern.id;
            return (
              <button key={pattern.id} onClick={() => setSelectedId(pattern.id)} className={`rounded-md border p-4 text-left transition-colors ${selectedCard ? "border-brand bg-brand/10" : "border-line bg-inset hover:border-line2"}`}>
                <div className="flex items-center justify-between gap-2"><TerminalBadge tone={stateTone(runtime.state)}>{runtime.state}</TerminalBadge><span className="font-mono text-[10px] text-faint">{pattern.decisionTf} · {pattern.family}</span></div>
                <PatternSketch kind={pattern.sketch} />
                <h2 className="text-[14px] font-semibold text-txt">{pattern.name}</h2>
                <p className="mt-2 min-h-[54px] text-[11px] leading-[18px] text-dim">{pattern.thesis}</p>
                <div className="mt-3 flex items-center justify-between gap-2 border-t border-line pt-3"><span className="font-mono text-[10px] text-faint">{runtime.armed} → {runtime.candidates} → {runtime.accepted}</span><TerminalBadge tone={evidence.tone}>{evidence.label}</TerminalBadge></div>
              </button>
            );
          })}
          {!filtered.length && <div className="col-span-full rounded-md border border-line bg-inset p-6 text-center font-mono text-[12px] text-dim">No published pattern matches those filters.</div>}
        </div>
      </TerminalPanel>

      <section className="grid gap-4 xl:grid-cols-[1.35fr_.65fr]">
        <TerminalPanel title={`Anatomy · ${selected.name}`} meta={`${selected.decisionTf} structure · ${selected.entryClock}`}>
          <div className="grid gap-4 md:grid-cols-[.75fr_1.25fr]">
            <div className="rounded-md border border-line bg-inset p-4">
              <PatternSketch kind={selected.sketch} />
              <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 text-[10px]"><span className="text-faint">REGIME</span><span className="text-right font-mono">{selected.regime}</span><span className="text-faint">DIRECTION</span><span className="text-right font-mono">{selected.direction}</span><span className="text-faint">STRUCTURE</span><span className="text-right font-mono">{selected.context}</span><span className="text-faint">ENTRY</span><span className="text-right font-mono">{selected.entryClock}</span><span className="text-faint">PROTECT</span><span className="text-right font-mono">{selected.protectionClock}</span></div>
            </div>
            <div>
              <div className="font-mono text-[10px] uppercase tracking-wider text-faint">Published test</div>
              <ol className="mt-3 space-y-3">{selected.rules.map((rule, index) => <li key={rule} className="flex gap-3 text-[12px] leading-5 text-dim"><span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-brand/40 font-mono text-[10px] text-brand">{index + 1}</span><span>{rule}</span></li>)}</ol>
              <div className="mt-4 grid gap-3 md:grid-cols-2"><div className="rounded-md border border-short/30 bg-short/5 p-3"><div className="font-mono text-[10px] uppercase text-short">Invalidation</div><p className="mt-2 text-[11px] leading-5 text-dim">{selected.invalidation}</p></div><div className="rounded-md border border-warn/30 bg-warn/5 p-3"><div className="font-mono text-[10px] uppercase text-warn">Economics</div><p className="mt-2 text-[11px] leading-5 text-dim">{selected.economics}</p></div></div>
            </div>
          </div>
          <div className="mt-4 rounded-md border border-line bg-bg/60 p-3 text-[11px] text-dim"><strong className="text-txt">Honest limitation.</strong> {selected.caution}</div>
        </TerminalPanel>
        <TerminalPanel title="Runtime match" meta={selectedRuntime.rows.length ? `${selectedRuntime.rows.length} lanes` : "not rostered"}>
          <div className="flex items-center justify-between"><div><div className="font-mono text-[10px] uppercase text-faint">Current setup state</div><div className="mt-2"><TerminalBadge tone={stateTone(selectedRuntime.state)}>{selectedRuntime.state}</TerminalBadge></div></div><div className="text-right"><div className="font-mono text-[10px] uppercase text-faint">Shadow booked</div><div className={`mt-1 font-mono text-xl ${selectedRuntime.netUsd < 0 ? "text-short" : selectedRuntime.netUsd > 0 ? "text-long" : "text-dim"}`}>{signedUsd(selectedRuntime.netUsd)}</div></div></div>
          <div className="my-4 grid grid-cols-4 overflow-hidden rounded-md border border-line bg-inset text-center"><div className="border-r border-line p-3"><span className="block font-mono text-lg">{selectedRuntime.armed}</span><span className="text-[9px] uppercase text-faint">armed</span></div><div className="border-r border-line p-3"><span className="block font-mono text-lg">{selectedRuntime.candidates}</span><span className="text-[9px] uppercase text-faint">candidate</span></div><div className="border-r border-line p-3"><span className="block font-mono text-lg">{selectedRuntime.accepted}</span><span className="text-[9px] uppercase text-faint">accepted</span></div><div className="p-3"><span className="block font-mono text-lg">{selectedRuntime.resolved}</span><span className="text-[9px] uppercase text-faint">resolved</span></div></div>
          <div className="font-mono text-[10px] uppercase text-faint">Why not now</div><div className="mt-2 space-y-1 text-[11px] text-dim">{selectedRuntime.waiting.length ? selectedRuntime.waiting.map((reason) => <div key={reason}>· {reason}</div>) : <div>· no active setup is recorded</div>}</div>
          <div className="mt-4 font-mono text-[10px] uppercase text-faint">Exact strategy IDs</div><div className="mt-2 flex flex-wrap gap-1">{selected.strategyIds.map((id) => <span key={id} className="rounded border border-line bg-bg px-2 py-1 font-mono text-[9px] text-dim">{id}</span>)}</div>
        </TerminalPanel>
      </section>

      <TerminalPanel title="The complete record" meta="failures shown beside survivors · exact IDs only">
        <DenseTable columns={recordColumns} rows={recordRows} rowKey={(row) => row.pattern.id} empty="Pattern evidence is unavailable." />
        {(lanesQuery.isError || scorecardQuery.isError) && <div className="mt-3 rounded-md border border-short/40 bg-short/5 p-3 text-[11px] text-short" role="alert">One or more evidence services are unavailable. Static anatomy is still visible, but runtime/evidence claims are incomplete.</div>}
      </TerminalPanel>

      <TerminalPanel title="How the perpetual pattern engine narrows the tape" meta="one clock per job">
        <div className="grid gap-3 md:grid-cols-5">
          {[
            ["01", "Regime", "Last-closed 1w / 1d / 4h decides the permitted family and side."],
            ["02", "Structure", "A closed 5m, 15m, or 1h bar creates a setup. Forming bars cannot arm."],
            ["03", "Acceptance", "BBO or the next open decides entry under the scanner's frozen clock contract."],
            ["04", "Economics", "Booked costs, safety wall, lot size, and risk are separate checks."],
            ["05", "Record", "Accepted, rejected, failed, and unresolved outcomes remain visible together."],
          ].map(([number, title, body]) => <div key={number} className="rounded-md border border-line bg-inset p-4"><div className="font-mono text-xl text-brand">{number}</div><h3 className="mt-3 text-[12px] font-semibold">{title}</h3><p className="mt-2 text-[10px] leading-5 text-dim">{body}</p></div>)}
        </div>
        <div className="mt-4 rounded-md border border-warn/30 bg-warn/5 p-3 text-[11px] text-dim"><strong className="text-warn">Measurement, not advice.</strong> Perpetuals add leverage, liquidation, funding, slippage, and venue risk. A visual pattern is not a recommendation and a backtest is not a live track record.</div>
      </TerminalPanel>
    </div>
  );
}
