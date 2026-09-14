import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../api";
import { AnalystEvidence, MarketConditions, observationState, type PublicObservation } from "./AnalystEvidence";
import "./CryptoAnalyst.css";

export interface AnalystMarket {
  symbol: string; timeframe: string; state: string; bias: string;
  alignment: number | null; coverage_pct: number; bars?: number;
  analysis_id: string | null; anchor_hash?: string; series_hash?: string;
  benchmark_ref?: { symbol: string; series_hash: string; as_of: string };
  as_of: string | null; issues: string[]; supports: string[]; conflicts: string[];
  metrics: Record<string, number | null>; setups: string[]; sparkline: number[];
  components: { name: string; weight: number; value: number | null; contribution: number | null }[];
  scenario?: { upside: string; downside: string; neutral: string };
  unavailable_inputs?: string[];
  market_evidence?: Record<string, PublicObservation>;
  history?: { required_bars: number; contiguous_bars: number; status: string; reason?: string | null; last_close?: string; recovery?: string };
}
export interface AnalystPayload {
  schema: string; spec_hash: string; generated_at: string; exchange: string; timeframe: string;
  session: string; brief: string;
  universe: { scope: string; discovered: number; displayed: number; current: number; truncated: boolean };
  breadth: { bullish: number; bearish: number; mixed: number; denominator: number; as_of?: string | null };
  markets: AnalystMarket[];
  public_universe?: { state: string; products: unknown[]; complete?: boolean };
  collector?: { stale: boolean; generated_at?: string; issues: string[] };
}

const SCANS = [
  { id: "all", name: "All markets", detail: "Public observations and technical coverage", icon: "◎" },
  { id: "upside_breakout", name: "Upside breakouts", detail: "Closed above the prior 20-bar range", icon: "↗" },
  { id: "downside_breakout", name: "Downside breaks", detail: "Closed below the prior 20-bar range", icon: "↘" },
  { id: "vwap_recovery", name: "VWAP recoveries", detail: "A bullish close through session value", icon: "∿" },
  { id: "compression", name: "Compression", detail: "Recent range volatility is contracting", icon: "⋈" },
  { id: "volume_expansion", name: "Volume expansion", detail: "At least 2× the prior volume baseline", icon: "▥" },
  { id: "trend_watch", name: "Trend watch", detail: "Price and EMA20/50 are aligned", icon: "⌁" },
];
const VENUES = [{ id: "delta_india", label: "Delta India" }, { id: "binanceusdm", label: "Binance Futures" }, { id: "bybit", label: "Bybit" }];
export function analystNumber(value: number | null | undefined, digits = 2): string {
  return value == null || !Number.isFinite(value) ? "—" : value.toLocaleString("en-US", { maximumFractionDigits: digits });
}
export function analystMatches(row: AnalystMarket, query: string, scan: string, bias: string): boolean {
  return row.symbol.toLowerCase().includes(query.toLowerCase()) && (scan === "all" || (row.state === "current" && row.setups.includes(scan)))
    && (bias === "all" || (row.state === "current" && row.bias === bias));
}
export function publicMarketValues(row: AnalystMarket, now: number, unavailable = false): Record<string, number | null> {
  const observation = row.market_evidence?.conditions;
  return !unavailable && observationState(observation, now) === "current" ? observation?.values ?? {} : {};
}
export function historyProgress(row: AnalystMarket): string {
  const h = row.history;
  if (row.issues.includes("no_canonical_analysis_in_covered_universe")) return "Canonical history not collected";
  if (!h) return human(row.issues[0] ?? row.state);
  if (h.status === "unverified") return `History unverified: ${human(h.reason ?? row.issues[0] ?? "missing proof")}`;
  return `${h.contiguous_bars} / ${h.required_bars} consecutive verified bars${h.status === "ready" ? " · window ready" : " · collecting"}`;
}
function human(value: string): string { return value.replace(/_/g, " "); }
function stamp(value: string | null | undefined): string {
  if (!value) return "No completed analysis";
  return new Date(value).toLocaleString("en-GB", { timeZone: "UTC", hour12: false }) + " UTC";
}
function Spark({ points, bias }: { points: number[]; bias: string }) {
  if (points.length < 2) return <span className="ca-muted">No series</span>;
  const min = Math.min(...points), range = Math.max(...points) - min || 1;
  const line = points.map((p, i) => `${i / (points.length - 1) * 130},${30 - (p-min) / range * 26}`).join(" ");
  return <svg className={`ca-spark ca-${bias}`} viewBox="0 0 130 34" role="img" aria-label="Last 40 closed bars"><polyline points={line} fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>;
}

export function CryptoAnalyst() {
  const [exchange, setExchange] = useState("delta_india");
  const [timeframe, setTimeframe] = useState("15m");
  const query = useQuery({ queryKey: ["crypto-analyst", exchange, timeframe],
    queryFn: () => apiGet<AnalystPayload>(`/api/crypto-analyst?${new URLSearchParams({ exchange, timeframe })}`),
    refetchInterval: 30_000, retry: 1 });
  return <CryptoAnalystView key={`${exchange}:${timeframe}`} data={query.data} error={query.isError}
    loading={query.isFetching} exchange={exchange} timeframe={timeframe} interactiveEvidence
    setExchange={setExchange} setTimeframe={setTimeframe} refresh={() => { void query.refetch(); }} />;
}

export function CryptoAnalystView({ data, error = false, loading = false, exchange = "delta_india", timeframe = "15m",
  setExchange = () => {}, setTimeframe = () => {}, refresh = () => {}, interactiveEvidence = false,
}: { data?: AnalystPayload; error?: boolean; loading?: boolean; exchange?: string; timeframe?: string;
  setExchange?: (value: string) => void; setTimeframe?: (value: string) => void; refresh?: () => void; interactiveEvidence?: boolean }) {
  const [view, setView] = useState("overview");
  const [scan, setScan] = useState("all");
  const [bias, setBias] = useState("all");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [watch, setWatch] = useState<string[]>([]);
  const [storageWarning, setStorageWarning] = useState(false);
  const [clock, setClock] = useState(Date.now());
  const watchKey = `vnedge.analyst.watch.v1.${exchange}`;
  useEffect(() => {
    setWatch([]); setStorageWarning(false);
    try { const saved: unknown = JSON.parse(localStorage.getItem(watchKey) ?? "[]");
      if (Array.isArray(saved)) setWatch(saved.filter((v): v is string => typeof v === "string" && /^[A-Z0-9]{3,30}$/.test(v)).slice(0, 100));
    } catch { setStorageWarning(true); }
  }, [watchKey]);
  useEffect(() => { const timer = window.setInterval(() => setClock(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const toggle = (symbol: string) => {
    const next = watch.includes(symbol) ? watch.filter(s => s !== symbol) : [...watch, symbol].slice(0, 100);
    setWatch(next);
    try { localStorage.setItem(watchKey, JSON.stringify(next)); } catch { setStorageWarning(true); }
  };
  const staleReport = !!data && clock - Date.parse(data.generated_at) > 90_000;
  const rows = useMemo(() => (data?.markets ?? []).filter(row => analystMatches(row, search, scan, bias)
    && (view !== "watchlist" || watch.includes(row.symbol))), [data, search, scan, bias, view, watch]);
  const active = rows.find(r => r.symbol === selected) ?? rows.find(r => r.state === "current") ?? rows[0];
  const current = data?.markets.filter(r => r.state === "current") ?? [];
  const publicCount = data?.markets.filter(r => Number.isFinite(publicMarketValues(r, clock, error || staleReport).mark_price)
    && publicMarketValues(r, clock, error || staleReport).mark_price != null).length ?? 0;
  const breadth = data?.breadth;
  const bullishShare = breadth?.denominator ? breadth.bullish / breadth.denominator * 100 : null;
  const bearishShare = breadth?.denominator ? breadth.bearish / breadth.denominator * 100 : null;
  const openScan = (id: string) => { setScan(id); setView("scanner"); };
  return <section className="ca-shell" aria-label="Crypto Analyst workspace">
    <aside className="ca-nav">
      <div className="ca-brand"><span className="ca-emblem">✳</span><div>VN / ANALYST<small>CRYPTO INTELLIGENCE</small></div></div>
      <span className="ca-overline">RESEARCH DESK</span>
      {[["overview", "◎", "Market overview"], ["scanner", "⌕", "Opportunity scanner"], ["watchlist", "☆", "My watchlist"], ["brief", "≋", "Analyst brief"]].map(([id, icon, label]) =>
        <button key={id} className={view === id ? "ca-nav-active" : ""} onClick={() => setView(id)} aria-pressed={view === id}><span>{icon}</span>{label}{id === "watchlist" && <small>{watch.length}</small>}</button>)}
      <div className="ca-nav-note"><span className="ca-overline">THE ANALYST'S JOB</span><p>Observe the market.<br />Explain the setup.<br />Show the other side.</p><small>Analysis is independent of the execution roster.</small></div>
      <div className="ca-authority">RESEARCH · NO ORDER ACCESS</div>
    </aside>
    <div className="ca-main">
      <header className="ca-toolbar"><div><span className="ca-overline">24/7 MARKETS / CLOSED-BAR ANALYSIS</span><h1>Crypto Analyst<span>.</span></h1></div>
        <div className="ca-controls"><label className="ca-sr" htmlFor="ca-venue">Venue</label><select id="ca-venue" value={exchange} onChange={e => setExchange(e.target.value)}>{VENUES.map(v => <option key={v.id} value={v.id}>{v.label}</option>)}</select>
        <label className="ca-sr" htmlFor="ca-tf">Analysis timeframe</label><select id="ca-tf" value={timeframe} onChange={e => setTimeframe(e.target.value)}>{["5m", "15m", "1h", "4h"].map(tf => <option key={tf}>{tf}</option>)}</select>
        <button className="ca-refresh" onClick={refresh} disabled={loading}>{loading ? "Updating…" : "↻ Refresh"}</button></div></header>
      <div className="ca-freshness"><span className={error || staleReport ? "ca-warning" : ""}>● {error ? "Connection unavailable" : staleReport ? "Report stale — historical view" : data ? "Connected · 30s refresh" : "Waiting for market data"}</span><span>{stamp(data?.generated_at)}</span><span>{data?.session ?? "UTC sessions · not exchange opening hours"}</span></div>
      {error && <div role="alert" className="ca-notice">Could not refresh analysis. Any previous report below is historical; it is not a live signal.</div>}
      {storageWarning && <div role="status" className="ca-notice">Watchlist storage is unavailable. Selections will last only for this visit.</div>}
      {data?.public_universe && <div className="ca-discovery-status"><span className="ca-overline">PUBLIC DISCOVERY</span> {data.public_universe.products.length} products · {data.public_universe.state} · {data.public_universe.complete ? "pagination complete" : "coverage incomplete"}<span>Technical ranks still require canonical bars.</span></div>}
      {data?.collector?.stale && <div className="ca-notice">Public observation worker is not current. Expired samples cannot support live market conclusions.</div>}
      {data && !current.length && <div role="status" className="ca-notice"><strong>Market data and technical analysis are separate.</strong> {publicCount} markets have fresh public mark prices. No technical profiles are ready on {timeframe}. History gaps and proof failures are listed per market; public prices do not replace canonical candles.</div>}
      <div className="ca-intro"><span className="ca-overline">{view === "overview" ? "THE BIG PICTURE" : view === "brief" ? "YOUR MARKET ANALYST" : "FIND WHAT DESERVES ATTENTION"}</span>
        <h2>{view === "overview" ? "Read the market. See the possibilities." : view === "watchlist" ? "Your focus, without the noise." : view === "brief" ? "One view. Both sides of the story." : "From market noise to a shortlist."}</h2>
        <p>Technical observations with reasons, reference levels and counter-evidence. No invented confidence. No automatic trades.</p></div>
      {(view === "overview" || view === "brief") && <>
        <div className="ca-market-grid">
          <article className="ca-card ca-pulse"><span className="ca-overline">MARKET BALANCE</span><h3>{!breadth?.denominator ? "Awaiting coverage" : breadth.bullish > breadth.bearish ? "Bullish participation leads" : breadth.bearish > breadth.bullish ? "Bearish participation leads" : "A divided market"}</h3>
            <div className="ca-breadth"><span style={{ width: `${bullishShare ?? 0}%` }} /><i style={{ width: `${bearishShare ?? 0}%` }} /></div>
            <div className="ca-between"><span className="ca-bullish">{analystNumber(bullishShare, 0)}% bullish</span><span className="ca-bearish">{analystNumber(bearishShare, 0)}% bearish</span></div>
            <p>{breadth?.denominator ?? 0} synchronized symbols · {breadth?.mixed ?? 0} mixed · equal-weight local coverage, not the whole crypto market.</p><small className="ca-muted">{breadth?.as_of ? stamp(breadth.as_of) : "No shared closed-bar observation"}</small></article>
          <article className="ca-card"><span className="ca-overline">MARKET DATA / TECHNICAL COVERAGE</span><div className="ca-big">{data ? publicCount : "—"}<small> / {data?.universe.displayed ?? "—"}</small></div><h3>Fresh public mark prices</h3><p>{data?.universe.current ?? 0} markets with current technical analysis. {data?.universe.discovered ?? 0} canonical symbol directories discovered. Public product discovery is not candle coverage.</p>{data?.universe.truncated && <span className="ca-warning">Canonical analysis universe capped at 24 markets</span>}</article>
          <article className="ca-card"><span className="ca-overline">PARTICIPATION</span><div className="ca-big">{data ? current.filter(r => r.setups.includes("volume_expansion")).length : "—"}<small> markets</small></div><h3>Relative volume ≥ 2×</h3><p>Versus the previous 20 bars. Participation is not proof of institutional activity.</p></article>
        </div>
        <article className="ca-brief"><span className="ca-brief-icon">✳</span><div><span className="ca-overline">ANALYST NOTE / EVIDENCE SUMMARY</span><p>{data?.brief ?? "Connect a covered market to build the first briefing. We will show what the data supports and what remains unknown."}</p><small>Rule-generated technical analysis · not an LLM forecast or a validated trading edge.</small></div></article>
      </>}
      {view !== "brief" && <div className="ca-scan-grid">{SCANS.map(s => <button key={s.id} className={scan === s.id ? "ca-scan ca-selected" : "ca-scan"} onClick={() => openScan(s.id)} aria-pressed={scan === s.id}><span className="ca-scan-icon">{s.icon}</span><strong>{s.name}</strong><small>{s.detail}</small><b>{s.id === "all" ? current.length : current.filter(r => r.setups.includes(s.id)).length}<span> ›</span></b></button>)}</div>}
      <div className="ca-workspace">
        <section className="ca-results"><div className="ca-results-head"><div><h3>{view === "watchlist" ? "My watchlist" : "Opportunity radar"}</h3><small>{rows.length} records · ranked by absolute alignment, not expected profit</small></div>
          <div className="ca-filters"><input aria-label="Search analyst symbols" placeholder="Search symbol…" value={search} onChange={e => setSearch(e.target.value)} /><select aria-label="Direction filter" value={bias} onChange={e => setBias(e.target.value)}><option value="all">Both directions</option><option value="bullish">Bullish</option><option value="bearish">Bearish</option><option value="mixed">Mixed</option></select><button onClick={() => { setScan("all"); setBias("all"); setSearch(""); }}>Reset</button></div></div>
          <p className="ca-muted">Public REST observations: mark price, spread and open interest. Separately: canonical closed price and technical profile. Expired public values are withheld.</p>
          <div className="ca-table-scroll"><table><thead><tr><th>Watch</th><th>Market</th><th>Public mark price</th><th>Spread (bps)</th><th>OI (USD)</th><th>Last closed price</th><th>12-bar move</th><th>Technical coverage</th><th>Alignment</th><th>Rel. volume</th><th>Recent tape</th></tr></thead><tbody>
            {rows.map(row => { const publicValues = publicMarketValues(row, clock, error || staleReport);
              const observation = row.market_evidence?.conditions;
              const publicState = error || staleReport ? "connection / report stale" : observationState(observation, clock);
              return <tr key={row.symbol} className={active?.symbol === row.symbol ? "ca-row-active" : ""}>
              <td><button className="ca-star" aria-label={`${watch.includes(row.symbol) ? "Unwatch" : "Watch"} ${row.symbol}`} aria-pressed={watch.includes(row.symbol)} onClick={() => toggle(row.symbol)}>{watch.includes(row.symbol) ? "★" : "☆"}</button></td>
              <td><button className="ca-symbol" onClick={() => setSelected(row.symbol)} aria-label={`Analyse ${row.symbol}`}>{row.symbol}<small>{row.state === "current" ? human(row.setups[0] ?? "observing") : human(row.issues[0] ?? row.state)}</small></button></td>
              <td>{analystNumber(publicValues.mark_price, 8)}<small className="ca-muted">{publicState} · {observation?.source ?? "No public source"}</small><small className="ca-muted">{observation?.venue_ts ? stamp(observation.venue_ts) : "No source timestamp"}</small></td>
              <td>{analystNumber(publicValues.spread_bps, 3)}</td><td>{analystNumber(publicValues.open_interest_usd, 0)}</td>
              <td>{analystNumber(row.metrics.price, 6)}<small className="ca-muted">{stamp(row.as_of)}</small></td><td className={(row.metrics.return_12_pct ?? 0) < 0 ? "ca-bearish" : "ca-bullish"}>{analystNumber(row.metrics.return_12_pct)}{row.metrics.return_12_pct != null && "%"}</td>
              <td><span className={`ca-chip ca-${row.state === "current" ? row.bias : "muted"}`}>{row.state === "current" ? row.bias : row.state}</span><small className="ca-muted">{historyProgress(row)}</small></td><td>{row.state === "current" ? analystNumber(row.alignment, 1) : "—"}<small className="ca-muted"> / ±100</small></td><td>{analystNumber(row.metrics.volume_ratio)}{row.metrics.volume_ratio != null && "×"}</td><td><Spark points={row.sparkline} bias={row.bias} /></td>
            </tr>; })}
          </tbody></table></div>
          {!rows.length && <div className="ca-empty"><span>◎</span><h3>{loading ? "Reading the market…" : view === "watchlist" ? "Build your focus list" : "No matching observations"}</h3><p>{view === "watchlist" ? "Star markets in the opportunity scanner. Your list is saved on this browser." : "Broaden the filters or check data coverage. An empty result is not a failed trade."}</p></div>}
        </section>
        <aside className="ca-detail" aria-label="Selected market analysis">
          {active ? <><div className="ca-between"><span className="ca-overline">SYMBOL DOSSIER</span><span className={`ca-chip ca-${active.bias}`}>{active.state}</span></div><h2>{active.symbol}</h2><p className="ca-muted">{timeframe} analysis · {stamp(active.as_of)}</p>
            <div className="ca-detail-score"><span className={`ca-${active.bias}`}>{analystNumber(active.alignment, 1)}</span><div>Directional alignment<small>−100 bearish → +100 bullish<br />{active.coverage_pct}% component coverage · not probability</small></div></div>
            <div className="ca-component-list">{active.components.map(c => <div key={c.name}><span>{human(c.name)}</span><div className="ca-meter"><i className={(c.contribution ?? 0) < 0 ? "negative" : ""} style={{ width: `${Math.abs(c.value ?? 0)*100}%` }} /></div><b>{analystNumber(c.contribution, 1)}<small> / {c.weight}</small></b></div>)}</div>
            <h4>What supports this view</h4>{active.supports.length ? active.supports.map((s, i) => <p className="ca-reason" key={i}>↗ {s}</p>) : <p className="ca-muted">No directional supporting evidence.</p>}
            <h4>The other side</h4>{active.conflicts.map((s, i) => <p className="ca-reason ca-warning" key={i}>↔ {s}</p>)}
            <h4>Reference levels</h4><dl className="ca-levels">{[["Prior range high", "range_high"], ["Prior range low", "range_low"], ["Exact session VWAP", "session_vwap"], ["ATR / price", "atr_pct"], ["Relative to BTC (pp)", "relative_btc_12_pct"]].map(([label, key]) => <div key={key}><dt>{label}</dt><dd>{analystNumber(active.metrics[key], key === "atr_pct" || key === "relative_btc_12_pct" ? 2 : 6)}</dd></div>)}</dl>
            {active.scenario && <><h4>What to watch next</h4><p>{active.scenario.upside}</p><p>{active.scenario.downside}</p><small>{active.scenario.neutral}</small></>}
            <h4>Technical history readiness</h4><p>{historyProgress(active)}</p>{active.history?.last_close && <p className="ca-muted">Latest verified close: {stamp(active.history.last_close)}</p>}{active.history?.recovery && active.state !== "current" && <p className="ca-muted">{active.history.recovery}</p>}
            <h4>Evidence gaps</h4>{active.issues.map(x => <p className="ca-warning" key={x}>{human(x)}</p>)}<p className="ca-muted">Technical calculation excludes public samples, settled funding and ML probabilities.</p>
            <MarketConditions observations={active.market_evidence} />
            <details><summary>Inspect source & methodology</summary><dl className="ca-proof"><dt>Analysis ID</dt><dd>{active.analysis_id ?? "Not issued"}</dd><dt>Anchor content hash</dt><dd>{active.anchor_hash ?? "Unavailable"}</dd><dt>Window digest</dt><dd>{active.series_hash ?? "Unavailable"}</dd><dt>BTC benchmark digest</dt><dd>{active.benchmark_ref?.series_hash ?? "Unavailable"}</dd><dt>Method version</dt><dd>{data?.schema} · {data?.spec_hash}</dd></dl><p>{active.bars ?? 0} contiguous closed bars; maximum 512. EMA20/50 are seeded on this bounded analysis window. Scores are fixed descriptive weights, not fitted probabilities. Volume direction uses candle colour, not aggressor classification.</p></details>
            <div className="ca-authority">EXECUTION & AFTER-COST EDGE: NOT ASSESSED</div>
          </> : <div className="ca-empty"><h3>Your analyst is ready</h3><p>Select a covered market to inspect its supporting evidence, contradictions and reference levels.</p></div>}
        </aside>
      </div>
      {interactiveEvidence && active && <AnalystEvidence key={`${exchange}:${active.symbol}`} exchange={exchange} symbol={active.symbol} />}
      <footer className="ca-footer"><span>ONE MARKET VIEW · MANY QUESTIONS</span><span>Canonical lake / closed bars / independent analysis · No automatic activation</span></footer>
    </div>
  </section>;
}
