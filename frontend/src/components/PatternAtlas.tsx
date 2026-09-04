import { useMemo, useState } from "react";
import type { PatternAtlasLane, PatternAtlasPattern, PatternFamily } from "../api";
import { usePatternAtlas } from "../queries";
import { DenseTable, TerminalBadge, TerminalPanel, type Column } from "./Terminal";

type Tone = "neutral" | "good" | "warn" | "bad" | "info";

function PatternSketch({ kind }: { kind: PatternAtlasPattern["sketch"] }) {
  const paths: Record<PatternAtlasPattern["sketch"], string> = {
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
    <svg viewBox="0 0 160 66" className="h-[72px] w-full" role="img" aria-label="Illustrative pattern anatomy, not market data">
      <path d="M4 54 H156 M4 34 H156 M4 14 H156" stroke="currentColor" className="text-line" strokeWidth="1" strokeDasharray="2 5" />
      <path d={paths[kind]} fill="none" stroke="currentColor" className="text-brand" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx="134" cy={kind === "sweep" ? "30" : kind === "reclaim" ? "25" : "15"} r="3" fill="currentColor" className="text-warn" />
    </svg>
  );
}

function opsTone(state: string): Tone {
  if (state === "ok") return "good";
  if (state === "degraded") return "warn";
  if (state === "blocked") return "bad";
  return "neutral";
}

function setupTone(state: string): Tone {
  if (state === "accepted" || state === "holding") return "good";
  if (state === "armed") return "info";
  if (state === "degraded") return "bad";
  if (state === "session_blocked") return "warn";
  return "neutral";
}

function evidenceTone(state: string): Tone {
  if (state === "sealed_pass" || state === "selection_pass") return "good";
  if (/killed|fail|negative/.test(state)) return "bad";
  if (state === "mixed" || state === "untested") return "warn";
  return "info";
}

function money(value: number) {
  return `${value < 0 ? "−" : value > 0 ? "+" : ""}$${Math.abs(value).toFixed(2)}`;
}

function millis(value: number | null) {
  if (value == null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)}s` : `${value.toFixed(0)}ms`;
}

function blockers(pattern: PatternAtlasPattern) {
  return [
    ...pattern.runtime.blockers.ops.map((reason) => `OPS · ${reason}`),
    ...pattern.runtime.blockers.setup.map((reason) => `SETUP · ${reason}`),
    ...pattern.runtime.blockers.evidence.map((reason) => `EVIDENCE · ${reason}`),
  ];
}

function LaneDiagnostic({ lane, onNavigate }: { lane: PatternAtlasLane; onNavigate: (tab: string) => void }) {
  const reasons = [...new Set([...lane.ops.reasons, ...lane.setup.reasons])].slice(0, 8);
  return (
    <article className="rounded-md border border-line bg-inset p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="font-mono text-[11px] font-semibold text-txt">{lane.symbol} · {lane.timeframe}</div>
          <div className="mt-1 font-mono text-[9px] text-faint">{lane.strategy_id} · {lane.exchange}</div>
        </div>
        <div className="flex flex-wrap gap-1">
          <TerminalBadge tone={opsTone(lane.ops.state)}>OPS {lane.ops.state}</TerminalBadge>
          <TerminalBadge tone={setupTone(lane.setup.state)}>SETUP {lane.setup.state}</TerminalBadge>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-4 overflow-hidden rounded border border-line bg-bg text-center">
        {[
          ["arm", lane.funnel.armed ?? 0],
          ["cand", lane.funnel.candidates ?? 0],
          ["acc", lane.funnel.accepted ?? 0],
          ["resolved", lane.funnel.resolved ?? 0],
        ].map(([label, value], index) => (
          <div key={String(label)} className={`${index < 3 ? "border-r border-line" : ""} p-2`}>
            <span className="block font-mono text-sm text-txt">{value}</span>
            <span className="text-[8px] uppercase text-faint">{label}</span>
          </div>
        ))}
      </div>
      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-[9px] font-mono">
        <span className="text-faint">close → arm</span><span className="text-right">{millis(lane.latency.close_to_arm_ms)}</span>
        <span className="text-faint">receipt / wait</span><span className="text-right">{millis(lane.latency.bar_close_receipt_ms)} / {millis(lane.latency.canonical_wait_ms)}</span>
        <span className="text-faint">decision</span><span className="text-right">{millis(lane.latency.decision_lag_ms)}</span>
        <span className="text-faint">quotes distinct / drop</span><span className="text-right">{lane.quotes.distinct} / {lane.quotes.overflow_drops}</span>
      </div>
      <div className="mt-3 min-h-8 text-[9px] leading-4 text-dim">
        {reasons.length ? reasons.map((reason) => <div key={reason}>· {reason}</div>) : <div>· operationally ready; no active setup recorded</div>}
      </div>
      <div className="mt-3 flex flex-wrap gap-1 border-t border-line pt-3">
        <button onClick={() => onNavigate("chart")} className="rounded border border-line px-2 py-1 font-mono text-[9px] text-brand hover:border-brand">chart</button>
        <button onClick={() => onNavigate("desk")} className="rounded border border-line px-2 py-1 font-mono text-[9px] text-brand hover:border-brand">lane</button>
        <button onClick={() => onNavigate("journal")} className="rounded border border-line px-2 py-1 font-mono text-[9px] text-brand hover:border-brand">journal</button>
        <button onClick={() => onNavigate("research")} className="rounded border border-line px-2 py-1 font-mono text-[9px] text-brand hover:border-brand">evidence</button>
      </div>
    </article>
  );
}

export function PatternAtlas({ onNavigate }: { onNavigate: (tab: string) => void }) {
  const atlasQuery = usePatternAtlas();
  const patterns = atlasQuery.data?.patterns ?? [];
  const [selectedId, setSelectedId] = useState("");
  const [family, setFamily] = useState<"all" | PatternFamily>("all");
  const [tf, setTf] = useState<"all" | PatternAtlasPattern["decision_tf"]>("all");
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => patterns.filter((pattern) => {
    const haystack = `${pattern.name} ${pattern.thesis} ${pattern.family} ${pattern.regime} ${pattern.strategy_ids.join(" ")}`.toLowerCase();
    return (family === "all" || pattern.family === family)
      && (tf === "all" || pattern.decision_tf === tf)
      && (!query.trim() || haystack.includes(query.trim().toLowerCase()));
  }), [family, patterns, query, tf]);
  const selected = patterns.find((pattern) => pattern.id === selectedId) ?? filtered[0] ?? patterns[0];

  if (atlasQuery.isLoading) {
    return <div className="rounded-md border border-line bg-panel p-10 text-center font-mono text-[11px] text-dim">Loading authoritative pattern contracts…</div>;
  }
  if (atlasQuery.isError || !selected) {
    return <div className="rounded-md border border-short/50 bg-short/5 p-6 font-mono text-[11px] text-short" role="alert">Pattern Atlas is unavailable. No static runtime claim is being substituted.</div>;
  }

  const summary = atlasQuery.data?.summary;
  const recordColumns: Column<PatternAtlasPattern>[] = [
    { key: "pattern", header: "Pattern", render: (row) => <button className="text-left" onClick={() => setSelectedId(row.id)}><span className="font-semibold text-txt">{row.name}</span><span className="block text-[9px] font-mono text-faint">{row.decision_tf} · {row.family}</span></button> },
    { key: "ops", header: "Operations", render: (row) => <TerminalBadge tone={opsTone(row.runtime.ops_state)}>{row.runtime.ops_state}</TerminalBadge> },
    { key: "setup", header: "Setup", render: (row) => <TerminalBadge tone={setupTone(row.runtime.setup_state)}>{row.runtime.setup_state}</TerminalBadge> },
    { key: "funnel", header: "Arm → candidate → accept", render: (row) => <span className="font-mono">{row.runtime.funnel.armed ?? 0} → {row.runtime.funnel.candidates ?? 0} → {row.runtime.funnel.accepted ?? 0}</span> },
    { key: "net", header: "Shadow booked", align: "right", render: (row) => <span className={`font-mono ${row.runtime.net_usd < 0 ? "text-short" : row.runtime.net_usd > 0 ? "text-long" : "text-dim"}`}>{money(row.runtime.net_usd)}</span> },
    { key: "evidence", header: "Evidence", render: (row) => <span><TerminalBadge tone={evidenceTone(row.evidence.state)}>{row.evidence.state}</TerminalBadge><span className="mt-1 block text-[9px] text-faint">{row.evidence.judgments} judgments · exact IDs</span></span> },
    { key: "why", header: "Current blockers", render: (row) => <span className="text-dim">{blockers(row).slice(0, 3).join(" · ") || "none reported"}</span> },
  ];

  return (
    <div className="space-y-4">
      <section className="overflow-hidden rounded-md border border-line bg-panel/70">
        <div className="grid lg:grid-cols-[1.25fr_.75fr]">
          <div className="p-5 md:p-7">
            <div className="font-mono text-[10px] font-bold uppercase tracking-[.2em] text-brand">VNEDGE Pattern Atlas · runtime diagnostic</div>
            <h1 className="mt-3 max-w-3xl text-2xl font-semibold leading-tight text-txt md:text-4xl">Pattern anatomy joined to the lanes that actually run it.</h1>
            <p className="mt-3 max-w-3xl text-[13px] leading-6 text-dim">Operations, setup lifecycle, and research evidence are separate truths. A healthy watcher can have no setup; an accepted setup can still have no validated edge.</p>
            <div className="mt-5 flex flex-wrap gap-2"><TerminalBadge tone="info">server-owned contracts</TerminalBadge><TerminalBadge tone="warn">probabilities, not predictions</TerminalBadge><TerminalBadge tone="neutral">read only</TerminalBadge><TerminalBadge tone="bad">capital locked</TerminalBadge></div>
          </div>
          <div className="grid grid-cols-4 border-t border-line bg-inset/60 lg:border-l lg:border-t-0">
            {[
              ["Patterns", summary?.patterns ?? patterns.length, "published"],
              ["Lanes", summary?.runtime_lanes ?? 0, "exact matches"],
              ["Active", summary?.active_setups ?? 0, "armed / accepted"],
              ["Accepted", summary?.accepted ?? 0, "cumulative"],
            ].map(([label, value, detail], index) => <div key={String(label)} className={`${index < 3 ? "border-r border-line" : ""} flex flex-col justify-center p-3`}><span className="font-mono text-[9px] uppercase text-faint">{label}</span><strong className="mt-2 font-mono text-2xl">{value}</strong><span className="mt-1 text-[8px] text-dim">{detail}</span></div>)}
          </div>
        </div>
      </section>

      <TerminalPanel title="Pattern screens" meta={`${filtered.length}/${patterns.length} shown · snapshot ${atlasQuery.data?.snapshot_state ?? "unknown"}`}>
        <div className="mb-4 flex flex-wrap gap-2">
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="search pattern, regime, scanner…" className="min-h-9 min-w-[250px] flex-1 rounded-md border border-line bg-bg px-3 text-[11px] font-mono text-txt placeholder:text-faint" />
          <select value={family} onChange={(event) => setFamily(event.target.value as typeof family)} className="min-h-9 rounded-md border border-line bg-bg px-3 text-[11px] font-mono"><option value="all">all families</option><option value="expansion">expansion</option><option value="continuation">continuation</option><option value="reclaim">reclaim</option><option value="reversal">reversal</option></select>
          <select value={tf} onChange={(event) => setTf(event.target.value as typeof tf)} className="min-h-9 rounded-md border border-line bg-bg px-3 text-[11px] font-mono"><option value="all">all clocks</option><option value="5m">5m</option><option value="15m">15m</option><option value="1h">1h</option></select>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {filtered.map((pattern) => <button key={pattern.id} onClick={() => setSelectedId(pattern.id)} className={`rounded-md border p-4 text-left transition-colors ${selected.id === pattern.id ? "border-brand bg-brand/10" : "border-line bg-inset hover:border-line2"}`}>
            <div className="flex flex-wrap items-center gap-1"><TerminalBadge tone={opsTone(pattern.runtime.ops_state)}>OPS {pattern.runtime.ops_state}</TerminalBadge><TerminalBadge tone={setupTone(pattern.runtime.setup_state)}>SETUP {pattern.runtime.setup_state}</TerminalBadge></div>
            <PatternSketch kind={pattern.sketch} />
            <h2 className="text-[14px] font-semibold text-txt">{pattern.name}</h2>
            <p className="mt-2 min-h-[54px] text-[11px] leading-[18px] text-dim">{pattern.thesis}</p>
            <div className="mt-3 flex items-center justify-between gap-2 border-t border-line pt-3"><span className="font-mono text-[10px] text-faint">{pattern.runtime.funnel.armed ?? 0} → {pattern.runtime.funnel.candidates ?? 0} → {pattern.runtime.funnel.accepted ?? 0}</span><TerminalBadge tone={evidenceTone(pattern.evidence.state)}>EVID {pattern.evidence.state}</TerminalBadge></div>
          </button>)}
          {!filtered.length && <div className="col-span-full rounded-md border border-line bg-inset p-6 text-center font-mono text-[12px] text-dim">No published pattern matches those filters.</div>}
        </div>
      </TerminalPanel>

      <section className="grid gap-4 xl:grid-cols-[.9fr_1.1fr]">
        <TerminalPanel title={`Anatomy · ${selected.name}`} meta={`${selected.decision_tf} structure · ${selected.entry_clock}`}>
          <PatternSketch kind={selected.sketch} />
          <div className="grid grid-cols-2 gap-x-4 gap-y-2 rounded-md border border-line bg-inset p-3 text-[10px]"><span className="text-faint">REGIME</span><span className="text-right font-mono">{selected.regime}</span><span className="text-faint">DIRECTION</span><span className="text-right font-mono">{selected.direction}</span><span className="text-faint">STRUCTURE</span><span className="text-right font-mono">{selected.context}</span><span className="text-faint">ENTRY</span><span className="text-right font-mono">{selected.entry_clock}</span><span className="text-faint">PROTECT</span><span className="text-right font-mono">{selected.protection_clock}</span></div>
          <ol className="mt-4 space-y-2">{selected.rules.map((rule, index) => <li key={rule} className="flex gap-3 text-[11px] leading-5 text-dim"><span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-brand/40 font-mono text-[9px] text-brand">{index + 1}</span><span>{rule}</span></li>)}</ol>
          <div className="mt-4 rounded-md border border-short/30 bg-short/5 p-3"><div className="font-mono text-[9px] uppercase text-short">Invalidation</div><p className="mt-2 text-[10px] leading-5 text-dim">{selected.invalidation}</p></div>
          <div className="mt-3 rounded-md border border-warn/30 bg-warn/5 p-3"><div className="font-mono text-[9px] uppercase text-warn">Economics / limitation</div><p className="mt-2 text-[10px] leading-5 text-dim">{selected.economics} {selected.caution}</p></div>
        </TerminalPanel>

        <TerminalPanel title="Exact lane diagnostics" meta={selected.runtime.lane_count ? `${selected.runtime.lane_count} lanes · no symbol aggregation` : "not rostered"}>
          <div className="mb-3 grid grid-cols-3 gap-2"><div className="rounded border border-line bg-inset p-2"><span className="block text-[8px] uppercase text-faint">Operations</span><span className="mt-1 block"><TerminalBadge tone={opsTone(selected.runtime.ops_state)}>{selected.runtime.ops_state}</TerminalBadge></span></div><div className="rounded border border-line bg-inset p-2"><span className="block text-[8px] uppercase text-faint">Setup</span><span className="mt-1 block"><TerminalBadge tone={setupTone(selected.runtime.setup_state)}>{selected.runtime.setup_state}</TerminalBadge></span></div><div className="rounded border border-line bg-inset p-2"><span className="block text-[8px] uppercase text-faint">Evidence</span><span className="mt-1 block"><TerminalBadge tone={evidenceTone(selected.evidence.state)}>{selected.evidence.state}</TerminalBadge></span></div></div>
          <div className="grid gap-3 lg:grid-cols-2">{selected.runtime.lanes.map((lane) => <LaneDiagnostic key={lane.lane_id} lane={lane} onNavigate={onNavigate} />)}{!selected.runtime.lanes.length && <div className="col-span-full rounded border border-line bg-inset p-5 text-center font-mono text-[10px] text-dim">No active runtime lane uses these exact strategy IDs.</div>}</div>
          <div className="mt-4 grid gap-3 md:grid-cols-3">{(["ops", "setup", "evidence"] as const).map((group) => <div key={group} className="rounded border border-line bg-bg p-3"><div className="font-mono text-[9px] uppercase text-faint">{group} blockers</div><div className="mt-2 text-[9px] leading-4 text-dim">{selected.runtime.blockers[group].length ? selected.runtime.blockers[group].map((reason) => <div key={reason}>· {reason}</div>) : <div>· none reported</div>}</div></div>)}</div>
          <div className="mt-4 font-mono text-[9px] uppercase text-faint">Exact-ID evidence</div><div className="mt-2 space-y-1">{selected.evidence.exact_ids.map((row) => <div key={row.strategy_id} className="flex flex-wrap items-center justify-between gap-2 rounded border border-line bg-bg px-2 py-1.5"><span className="font-mono text-[9px] text-dim">{row.strategy_id}</span><span className="flex items-center gap-2"><span className="text-[8px] text-faint">{row.judgments} judgments</span><TerminalBadge tone={evidenceTone(row.state)}>{row.state}</TerminalBadge></span></div>)}</div>
        </TerminalPanel>
      </section>

      <TerminalPanel title="Complete record" meta="operations ≠ setup ≠ evidence">
        <DenseTable columns={recordColumns} rows={patterns} rowKey={(row) => row.id} empty="Pattern evidence is unavailable." />
      </TerminalPanel>

      <TerminalPanel title="How the engine narrows the tape" meta="one clock per job">
        <div className="grid gap-3 md:grid-cols-5">{[
          ["01", "Regime", "Last-closed 1w / 1d / 4h permits a family and side."],
          ["02", "Structure", "A closed 5m, 15m, or 1h bar may create a setup."],
          ["03", "Acceptance", "BBO or next-open applies the frozen entry clock."],
          ["04", "Economics", "Booked cost and the conservative gate remain separate."],
          ["05", "Record", "Every reject and unresolved outcome remains visible."],
        ].map(([number, title, body]) => <div key={number} className="rounded-md border border-line bg-inset p-4"><div className="font-mono text-xl text-brand">{number}</div><h3 className="mt-3 text-[12px] font-semibold">{title}</h3><p className="mt-2 text-[10px] leading-5 text-dim">{body}</p></div>)}</div>
        <div className="mt-4 rounded-md border border-warn/30 bg-warn/5 p-3 text-[11px] text-dim"><strong className="text-warn">Measurement, not advice.</strong> A visual pattern is not a recommendation and a backtest is not a live track record.</div>
      </TerminalPanel>
    </div>
  );
}
