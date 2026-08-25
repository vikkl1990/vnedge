// Browser authentication never places a credential in the URL. The root
// token is submitted once in an Authorization header and exchanged for a
// short-lived HttpOnly cookie; all later HTTP/WebSocket calls use that cookie.

export interface BrowserSession {
  expires_at: string | null;
}

export interface ReadinessStatus {
  status: "ready" | "not_ready" | "unknown";
  reasons: string[];
  http_status: number;
}

export async function hasBrowserSession(): Promise<BrowserSession | null> {
  const response = await fetch("/whoami", {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) return null;
  const body = await response.json() as { expires_at?: string | null };
  return { expires_at: body.expires_at ?? null };
}

export async function establishBrowserSession(rootToken: string): Promise<BrowserSession> {
  const token = rootToken.trim();
  if (!token) throw new ApiError(401, "token is required");
  const response = await fetch("/auth/session", {
    method: "POST",
    credentials: "same-origin",
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) throw new ApiError(response.status, "authentication failed");
  const body = await response.json() as { expires_at?: string | null };
  return { expires_at: body.expires_at ?? null };
}

const SESSION_REFRESH_MS = 8 * 60 * 1000;
const SESSION_EXPIRY_SKEW_MS = 60 * 1000;
const MIN_SESSION_REFRESH_MS = 5 * 1000;

async function refreshBrowserSession(): Promise<BrowserSession> {
  const csrf = cookie("vnedge_csrf");
  if (!csrf) {
    window.dispatchEvent(new Event("vnedge-auth-expired"));
    throw new ApiError(401, "session CSRF state is unavailable");
  }
  const response = await fetch("/auth/session/refresh", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-VNEDGE-CSRF": csrf },
    cache: "no-store",
  });
  if (response.status === 401 || response.status === 403) {
    window.dispatchEvent(new Event("vnedge-auth-expired"));
    throw new ApiError(response.status, "session expired — authenticate again");
  }
  if (!response.ok) throw new ApiError(response.status, "session refresh failed");
  const body = await response.json() as { expires_at?: string | null };
  return { expires_at: body.expires_at ?? null };
}

function refreshDelay(expiresAt: string | null): number {
  if (!expiresAt) return SESSION_REFRESH_MS;
  const expiry = Date.parse(expiresAt);
  if (!Number.isFinite(expiry)) return SESSION_REFRESH_MS;
  return Math.max(
    MIN_SESSION_REFRESH_MS,
    Math.min(SESSION_REFRESH_MS, expiry - Date.now() - SESSION_EXPIRY_SKEW_MS),
  );
}

export function keepBrowserSessionAlive(
  initialExpiresAt: string | null,
  onRefresh: (session: BrowserSession) => void,
): () => void {
  let stopped = false;
  let expiresAt = initialExpiresAt;
  let timer = 0;
  const schedule = (delay = refreshDelay(expiresAt)) => {
    window.clearTimeout(timer);
    if (!stopped) timer = window.setTimeout(() => void refresh(), delay);
  };
  const refresh = async () => {
    if (stopped) return;
    if (document.visibilityState !== "visible") {
      schedule(30_000);
      return;
    }
    try {
      const session = await refreshBrowserSession();
      expiresAt = session.expires_at;
      onRefresh(session);
    } catch (error) {
      if (!(error instanceof ApiError) || (error.status !== 401 && error.status !== 403)) {
        schedule(30_000);
      }
      return;
    }
    schedule();
  };
  const focus = () => schedule(MIN_SESSION_REFRESH_MS);
  schedule();
  window.addEventListener("focus", focus);
  return () => {
    stopped = true;
    window.clearTimeout(timer);
    window.removeEventListener("focus", focus);
  };
}

function cookie(name: string): string {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (method !== "GET" && method !== "HEAD") {
    const csrf = cookie("vnedge_csrf");
    if (csrf) headers.set("X-VNEDGE-CSRF", csrf);
  }
  const res = await fetch(path, {
    ...init,
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (res.status === 401) {
    window.dispatchEvent(new Event("vnedge-auth-expired"));
    throw new ApiError(401, "unauthorized — start a new session");
  }
  if (!res.ok) {
    let message = `request failed (${res.status})`;
    try {
      const body = await res.json() as { detail?: string };
      if (body.detail) message = body.detail;
    } catch { /* non-JSON error */ }
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  return apiRequest<T>(path);
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  return apiRequest<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return apiRequest<T>(path, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export async function apiDelete(path: string): Promise<void> {
  return apiRequest<void>(path, { method: "DELETE" });
}

// --- Response shapes (only the fields the panels read) ---------------------
/** Canonical OHLCV for the chart.
 *
 * Served from the same store research and shadow read. The UI deliberately does
 * NOT derive candles of its own: three candle sources already disagree in this
 * system, and a fourth built for display would be the hardest to notice.
 */
export type ChartTimeframe = "1m" | "5m" | "15m" | "1h" | "4h";

export interface ChartCandle {
  time: number;      // epoch SECONDS — the unit lightweight-charts expects
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface ChartCandles {
  symbol: string;
  timeframe: string;
  source: string;    // "canonical_lake"
  count: number;
  truncated: boolean;
  candles: ChartCandle[];
}

export interface ChartMarker {
  time: number;
  position: "aboveBar" | "belowBar";
  shape: "arrowUp" | "arrowDown" | "circle";
  color: string;
  text: string;
}

export interface ChartMarkers {
  symbol: string;
  count: number;
  journals: number;
  markers: ChartMarker[];
}

export async function fetchChartCandles(
  symbol: string,
  timeframe: ChartTimeframe,
  n = 500,
  exchange = "binanceusdm",
): Promise<ChartCandles> {
  const q = new URLSearchParams({ timeframe, n: String(n), exchange });
  return apiGet<ChartCandles>(`/api/candles/${encodeURIComponent(symbol)}?${q}`);
}

/** Where the lanes actually got in and out, for overlay on the candles. */
export async function fetchChartMarkers(
  symbol: string,
  n = 500,
): Promise<ChartMarkers> {
  const q = new URLSearchParams({ n: String(n) });
  return apiGet<ChartMarkers>(
    `/api/candles/${encodeURIComponent(symbol)}/markers?${q}`,
  );
}

export interface WhoAmI {
  name: string | null;
  role: string | null;
  permissions: string[];
  expires_at?: string | null;
}

export interface SettingsSecurity {
  session: string;
  secrets_store_ready: boolean;
  live_controls_available: false;
  operator_id: string;
}

export interface OperatorProfile {
  operator_id: string;
  display_name: string;
  timezone: string;
  created_at: string;
  updated_at: string;
}

export type ExchangeId = "binanceusdm" | "bybit" | "delta_india";
export type KeyPurpose = "read" | "trade";
export type ConnectionStatus = "not_configured" | "configured" | "verified" | "invalid" | "disabled";

export interface ExchangeConnectionPublic {
  exchange: ExchangeId;
  purpose: KeyPurpose;
  status: ConnectionStatus;
  api_key_hint: string;
  permissions_note: string;
  last_verified_at: string | null;
  last_error: string | null;
  can_trade: boolean;
  private_stream: string;
}

export interface Position {
  symbol?: string;
  side?: string;
  quantity?: number;
  entry_price?: number;
  mark_price?: number;
  notional_usd?: number;
  margin_usd?: number;
  effective_leverage?: number;
  liquidation_price?: number;
  stop_price?: number;
  take_profit_price?: number;
  age_seconds?: number;
  mfe_usd?: number;
  mae_usd?: number;
  unrealized_pnl_usd?: number;
  [k: string]: unknown;
}

export interface PriceBook {
  bid?: number;
  ask?: number;
  mid?: number;
  spread_bps?: number;
}

export interface FeedHealth {
  exchange?: string;
  candles?: string;
  funding?: string;
  open_interest?: string;
  last_update_ms?: number;
}

// Health blocks the classic cockpit already renders — typed here so React stops
// ignoring them (the D-lite / candle-path schema). All optional + null-safe.
export interface TimeMachine {
  health?: Record<string, string>;
  age_ms?: Record<string, number>;
  forming?: Record<string, { progress?: number }>;
}
export interface LatencyStat {
  last?: number;
  p50?: number;
  p95?: number;
  max?: number;
  n?: number;
  recent?: number[];
}
export interface LatencyRecoveryState {
  state?: "unknown" | "warming" | "nominal" | "blocked" | "recovering" | "recovered";
  raw_band?: string;
  effective_band?: string;
  healthy_samples?: number;
  required_samples?: number;
  recovery_threshold_ms?: number;
}
export interface RegimeReading {
  label?: string;
  confidence?: number;
  allow_long?: boolean;
  allow_short?: boolean;
}
export interface PlanOverlay {
  side?: string;
  expected_net_bps?: number;
  gate_ok?: boolean;
}
export interface TrialCriterion {
  name?: string;
  value?: number;
  threshold?: number;
  ok?: boolean;
  hard?: boolean;
  unit?: string;
}
export interface TrialScorecard {
  trial_id?: string;
  verdict?: string;
  criteria?: TrialCriterion[];
}
export interface ScannerRuntimeContract {
  cost_family?: string;
  max_holding_bars?: number;
  max_holding_hours?: number;
  decision_engine?: string;
  exit_engine?: string;
  rationale?: string;
}
export interface LaneRow {
  lane_id?: string;
  strategy_id?: string;
  symbol?: string;
  timeframe?: string;
  mode?: string;
  cost_profile?: string;
  feed?: string;
  time_machine?: TimeMachine | null;
  latency?: Record<string, LatencyStat> | null;
  latency_recovery?: Record<string, LatencyRecoveryState> | null;
  decision_skips?: Record<string, number> | null;
  regime?: RegimeReading | null;
  plan_overlay?: PlanOverlay | null;
  equity?: number;
  peak_equity?: number;
  drawdown_pct?: number | null;
  dd_limit_pct?: number | null;
  trial_scorecard?: TrialScorecard | null;
  bands?: { age?: string; bar_close_lag?: string; decision_lag?: string; dd?: string; verdict_tone?: string } | null;
}

export interface CorrectionLane {
  lane_id: string;
  strategy_id: string;
  eligibility: "eligible" | "KILLED" | "RESEARCH_ONLY" | "unknown";
  mode: "shadow" | "paper" | "measurement" | "off";
  observation_class: "shadow_observe" | "measurement" | null;
  exchange: string;
  candle_source: string;
  symbol: string;
  timeframe: string;
  capital: boolean;
  venue_rtt_ms: number | null;
  candle_status: string;
  candle_age_ms: number | null;
  bar_close_processing_ms: number | null;
  bar_close_receipt_ms: number | null;
  canonical_wait_ms: number | null;
  decision_lag_ms: number | null;
  latency_samples: { bar_close: number; canonical_wait: number; decision: number; required: number };
  latency_recovery: Record<string, LatencyRecoveryState>;
  arm_skips: number;
  last_signal_age_seconds: number | null;
  last_signal_reason: string;
  current_waiting_reason: string;
  cost_profile: string;
  round_trip_bps: number | null;
  health: "ok" | "degraded" | "blocked" | "unknown";
  health_reason: string | null;
  equity_usd: number | null;
  realized_pnl_usd: number | null;
  unrealized_pnl_usd: number | null;
  open_positions: number;
  funnel: Record<string, number>;
  sizing_profile: SizingProfile | null;
  runtime_contract: ScannerRuntimeContract | null;
  active_plan: Record<string, unknown> | null;
  last_eval: Record<string, unknown> | null;
  why_no_fire: string;
  last_reject_reason: string | null;
  shadow_perf: {
    pending_shadow_intents?: number;
    pending_intents?: Array<{
      intent_key: string;
      side: string;
      entry_price: number;
      stop_price: number;
      take_profit_price: number | null;
      decision_bar_ts: string;
      signal_reason: string;
    }>;
    shadow_outcomes_recent?: Record<string, unknown>[];
    virtual_net_usd?: number;
    wins?: number;
    losses?: number;
    profit_factor?: number | null;
    bars_since_signal?: number | null;
    acceptance_state?: string;
    quote_source?: string;
    quote_ingest_lag_seconds?: number;
    quotes_seen?: number;
    quotes_distinct?: number;
    quote_contract_rejects?: number;
    quote_overflow_drops?: number;
    quote_rearms?: number;
    overflow_probe_resets?: number;
  } | null;
}

export interface LanesPayload {
  generated_at: string;
  source_snapshot_at: string | null;
  lanes: CorrectionLane[];
  capital_roster_size: number;
  measurement_only: boolean;
  banner: string | null;
  shadow_observe_lanes: number;
  shadow_observe_strategies: string[];
  shadow_observe_timeframes: string[];
  lane_set_hash: string | null;
  portfolio: PortfolioScope;
  scanner_conflicts: Record<string, {
    state: "empty" | "single" | "aligned" | "conflict";
    selected: string | null;
    side?: string;
    read_only?: boolean;
    candidates: Array<Record<string, unknown>>;
  }>;
  read_only: true;
  can_promote: false;
  can_trade: false;
}

export interface PortfolioScope {
  shadow_purse_usd: number;
  paper_purse_usd: number;
  measurement_nominal_usd: number;
  shadow_lane_count: number;
  paper_lane_count: number;
  measurement_lane_count: number;
  shadow_open_positions: number;
  shadow_pending_intents: number;
}

export interface SizingProfile {
  starting_equity_usd?: number;
  fixed_margin_usd?: number | null;
  max_leverage?: number;
  max_effective_account_leverage?: number;
  max_symbol_exposure_usd?: number;
  max_total_exposure_usd?: number;
  max_open_positions?: number;
  daily_loss_halt_enabled?: boolean;
  profile?: string;
}

export interface RiskSnapshot {
  generated_at: string;
  source_snapshot_at: string | null;
  runtime_mode: "measurement" | "shadow" | "paper" | "live_blocked" | string;
  runtime_label: string;
  capital: { enabled: boolean; roster_size: number };
  build_sha: string;
  kill: { active: boolean; latched: boolean };
  feed: { status: "healthy" | "stale" | "gap" | "unknown"; label: string };
  live: {
    blocked: boolean;
    message: string | null;
    delta_private_status: "not_implemented" | "connected" | "degraded";
  };
  daily_halt: {
    used_usd: number;
    limit_usd: number | null;
    used_pct_of_peak_equity: number | null;
    active: boolean;
  };
  journal: {
    available: boolean;
    recovery_degraded: boolean;
    quarantine_path: string | null;
    recovery_error: string | null;
    entries_blocked: boolean;
  };
  gateway: {
    last_reject_reasons: { reason: string; count: number }[];
    observed_reject_count: number;
    window: string;
  };
  positions: { shadow_open: number; shadow_pending_intents: number; unresolved_orders: number };
  portfolio: PortfolioScope;
  sizing_profiles: Array<SizingProfile & { lane_id: string; symbol: string }>;
  breaker: { loss_streak: number; active: boolean; threshold: number };
  reconciliation: {
    status: string;
    last_success_at: string | null;
    last_success_age_seconds: number | null;
    fail_count: number;
    clean: boolean;
  };
  live_checklist: {
    passed: number;
    total: number;
    items: { id: string; label: string; ok: boolean }[];
  };
  streams: {
    exchange: string;
    public_feed: string;
    private_stream: "not_implemented" | "not_required" | "connected" | "degraded";
  }[];
  read_only: true;
  can_trade: false;
}

export interface Chip {
  band?: string;
  label?: string;
}

export interface LaneHealthProblem {
  lane_id?: string;
  verdict?: string;
  age_seconds?: number | null;
  detail?: string;
  trade_compatible?: boolean;
}

export interface LaneHealth {
  healthy?: boolean;
  production_healthy?: boolean;
  summary?: string;
  production_summary?: string;
  totals?: Record<string, number>;
  problems?: LaneHealthProblem[];
}

export interface Snapshot {
  mode?: string;
  symbol?: string;
  price?: PriceBook | null;
  funding_rate?: number | null;
  feed_health?: FeedHealth;
  equity?: number;
  peak_equity?: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  daily_pnl?: number;
  consecutive_losses?: number;
  kill_switch_active?: boolean;
  live_trading_enabled?: boolean;
  risk_status?: string;
  positions?: Position[];
  fills?: number;
  fees_usd?: number;
  // candle-path / D-lite health blocks
  snapshot_age_ms?: number | null;
  lanes?: LaneRow[];
  time_machine?: TimeMachine | null;
  latency?: Record<string, LatencyStat> | null;
  decision_skips?: Record<string, number> | null;
  cost_profile?: string | null;
  regime?: RegimeReading | null;
  plan_overlay?: PlanOverlay | null;
  session?: Record<string, unknown>;
  chips?: Record<string, Chip>;
  lane_health?: LaneHealth | null;
  [k: string]: unknown;
}

export interface JournalRow {
  lane?: string;
  symbol?: string;
  side?: string;
  net_pnl_usd?: number;
  net_after_this_fill_fee_usd?: number;
  virtual_net_usd?: number;
  fee_usd?: number;
  fees_usd?: number;
  exit_reason?: string;
  resolution?: string;
  [k: string]: unknown;
}

export interface ScannerAuditEvent {
  lane: string;
  ts: string;
  bar_ts: string;
  entry_ts?: string | null;
  kind: "signal" | "evaluation" | "entry" | "rejection" | "exit";
  source_event: string;
  intent_key?: string;
  strategy_id: string;
  exchange?: string;
  symbol: string;
  timeframe: string;
  side: string;
  price: number | null;
  decision_price?: number | null;
  entry_price?: number | null;
  stop_price?: number | null;
  target_price?: number | null;
  approved: boolean;
  reason: string;
  resolution?: string;
  virtual_net_usd?: number;
  bars_held?: number;
  backfill?: boolean;
}

export interface JournalPayload {
  generated_at: string;
  summary: {
    positions: number;
    open_orders: number;
    fills: number;
    closed_trades: number;
    actual_realized_pnl_usd: number;
    actual_closed_net_usd: number;
    actual_closed_trades: number;
    shadow_closed_trades: number;
    scanner_events: number;
    fees_usd: number;
    virtual_net_usd: number;
    [k: string]: unknown;
  };
  closed_trades: JournalRow[];
  scanner_events: ScannerAuditEvent[];
  events: Array<{
    lane?: string;
    ts?: string;
    event?: string;
    detail?: string;
    [k: string]: unknown;
  }>;
  orders: Array<Record<string, unknown>>;
  fills: Array<Record<string, unknown>>;
  page?: {
    offset: number;
    limit: number;
    totals: { fills: number; orders: number; closed_trades: number; events: number; scanner_events: number };
    has_previous: boolean;
    has_more: boolean;
  };
}

export interface MetaPayload {
  generated_at?: string;
  build_sha: string;
  host: string;
  uptime_seconds: number;
  process_id?: number;
  python?: string;
  cpu_count?: number | null;
  load_average?: { "1m": number | null; "5m": number | null; "15m": number | null };
  disk?: { total_bytes: number; used_bytes: number; free_bytes: number; used_pct: number };
  transport?: { scheme: string; secure: boolean; forwarded_proto: string | null };
}

export interface CostModelPayload {
  taker_round_trip_cost_bps: number;
  maker_first_cost_bps: number;
  exchanges: Array<{
    exchange: string;
    label: string;
    taker_round_trip_cost_bps: number;
  }>;
}

export interface ArtifactMetadata {
  available: boolean;
  state: "CURRENT" | "STALE" | "MISSING" | "UNKNOWN" | "HISTORICAL";
  served_at: string;
  source_as_of: string | null;
  age_seconds: number | null;
  expected_interval_seconds: number | null;
  historical_evidence: boolean;
}

export interface DataProductsPayload {
  generated_at: string;
  required_non_current: number;
  rows: Array<Partial<ArtifactMetadata> & {
    product: string;
    class: string;
    required: boolean;
    state: ArtifactMetadata["state"];
    age_seconds: number | null;
    expected_interval_seconds: number | null;
    source_as_of: string | null;
  }>;
  read_only: true;
}

export interface BacktestRunSummary {
  run_id: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  strategy_id: string | null;
  exchange: string | null;
  symbol: string | null;
  timeframe: string | null;
  net_profit_usd: number | null;
  num_trades: number | null;
  has_report: boolean;
  blocked_reason?: string | null;
  error?: string | null;
  execution?: string | null;
}

export interface BacktestCurvePoint {
  ts: string;
  equity_usd: number;
  drawdown_pct: number;
}

export interface BacktestTrade {
  side: string;
  quantity: number;
  entry_ts: string;
  entry_price: number;
  exit_ts: string;
  exit_price: number;
  exit_reason: string;
  entry_reason: string;
  gross_pnl_usd: number;
  fees_usd: number;
  funding_usd: number;
  net_pnl_usd: number;
  net_bps_on_entry_notional: number | null;
  return_on_initial_equity_pct: number;
  mae_usd: number;
  mfe_usd: number;
  hold_seconds: number;
}

export interface BacktestDay {
  date: string;
  net_pnl_usd: number;
  trade_count: number;
  wins: number;
  losses: number;
  equity_usd: number;
  equity_change_usd: number;
  drawdown_pct: number;
}

export interface BacktestMonth {
  month: string;
  net_pnl_usd: number;
  traded_days: number;
  trade_count: number;
  win_days: number;
  loss_days: number;
  best_day_usd: number;
  worst_day_usd: number;
  max_drawdown_pct: number;
  close_equity_usd: number;
}

export interface BacktestOverview {
  num_trades: number;
  skipped_by_sizing: number;
  net_profit_usd: number;
  gross_profit_usd: number;
  total_cost_usd: number;
  return_pct: number;
  annualized_return_pct: number | null;
  max_drawdown_pct: number;
  sharpe: number;
  sortino: number;
  calmar: number | null;
  profit_factor: number | null;
  win_rate_pct: number;
  avg_win_usd: number;
  avg_loss_usd: number;
  payoff_ratio: number;
  total_fees_usd: number;
  total_funding_usd: number;
  exit_reasons: Record<string, number>;
  avg_mae_usd: number;
  avg_mfe_usd: number;
  traded_days: number;
  win_days: number;
  loss_days: number;
  avg_day_pnl_usd: number;
  median_day_pnl_usd: number;
  best_day_usd: number;
  worst_day_usd: number;
  best_trade_usd: number;
  worst_trade_usd: number;
  max_win_streak: number;
  max_loss_streak: number;
  longest_underwater_days: number;
  avg_hold_hours: number;
  best_trade_profit_share_pct: number | null;
}

export interface BacktestReport {
  schema: "vnedge.backtest_report.v1";
  run: {
    run_id: string;
    status: string;
    generated_at: string;
    engine: string;
    evidence_class: string;
    strategy_id: string;
    exchange: string;
    symbol: string;
    timeframe: string;
    data_source: string;
    bars: number;
    window: { start: string | null; end: string | null; duration_days: number };
    parameters: Record<string, unknown>;
    initial_equity_usd: number;
    costs: {
      maker_bps_per_leg: number;
      taker_bps_per_leg: number;
      slippage_bps_per_leg: number;
      modeled_taker_round_trip_bps: number;
      funding_included: boolean;
    };
    exit_contract: {
      max_holding_bars: number;
      active_exit: boolean;
      partial_take_profit: boolean;
      trail_atr_mult: number;
      fee_aware_breakeven_bps: number;
    };
  };
  overview: BacktestOverview;
  equity_curve: BacktestCurvePoint[];
  daily: BacktestDay[];
  monthly: BacktestMonth[];
  trades: BacktestTrade[];
  warnings: string[];
  governance: {
    can_trade: false;
    can_promote: false;
    read_only: true;
    promotion_requires_separate_untouched_judgment: boolean;
  };
}

export interface BacktestLabPayload {
  lab_id: string;
  selected_run_id: string | null;
  selected: BacktestReport | null;
  selected_summary: BacktestRunSummary | null;
  runs: BacktestRunSummary[];
  catalog: {
    strategies: string[];
    exchanges: string[];
    symbols: string[];
    timeframes: string[];
  };
  submission: {
    mode: "AGENT_GATEWAY_JOB";
    inline_execution: false;
    reason: string;
    worker_command: string;
  };
  read_only: true;
  can_trade: false;
  can_promote: false;
}

export interface BacktestJobAccepted {
  job_id: string;
  kind: "backtest_request";
  status: string;
  created_at: string;
  updated_at: string;
  created_by: string;
  can_trade: false;
  can_promote: false;
  live_orders_enabled: false;
}

export interface ResearchScorecard {
  generated_at: string | null;
  strategies: Array<{
    strategy: string;
    best_net_bps: number | null;
    oos_net_bps: number | null;
    verdict: string | null;
    source_verdict: string | null;
    metric_state: "SAMPLE_QUALIFIED" | "UNDER_SAMPLED";
    sample_qualified: boolean;
    profit_factor: number | null;
    profit_factor_display: string;
    profit_factor_reason: string | null;
    sharpe: number | null;
    sharpe_reason: string | null;
    sharpe_convention: string;
    deflated_sharpe: number | null;
    deflated_sharpe_gate: number;
    deflated_sharpe_pass: boolean;
    raw_trials: number | null;
    effective_trials: number | null;
    trial_count_reason: string | null;
    max_drawdown_pct: number | null;
    break_rate_pct: number | null;
    samples: number;
    samples_total: number;
    sample_unit: string;
    min_samples: number;
    metrics_after_cost: true;
    venues: string[];
  }>;
  performance_policy: {
    min_samples: number;
    sample_rule: string;
    profit_factor_basis: string;
    sharpe_basis: string;
    deflated_sharpe_gate: number;
    trial_disclosure_rule: string;
    ranking_rule: string;
  };
  runtime_alignment?: Array<{
    strategy_id: string;
    lane_count: number;
    symbols: string[];
    timeframes: string[];
    resolved_outcomes: number;
    pending_intents: number;
    scorecard_match: boolean;
    status: "EVIDENCE_MATCH" | "RUNTIME_OUTCOMES_NOT_SCORED" | "NO_CURRENT_EVIDENCE";
  }>;
  can_trade: false;
  can_promote: false;
  artifact?: ArtifactMetadata;
}

export interface StrategyWorkflowRevision {
  revision_id: string;
  strategy_id: string;
  version: string;
  parent_revision_id: string | null;
  stage: string;
  status: string;
  status_reason: string;
  timeframes: string[];
  symbols: string[];
  backtest_engine: string;
  engine_version: string;
  parity_status: "PASS" | "FAIL" | "NOT_REPORTED";
  preregistration: string;
  governance_flags: string[];
  performance: {
    after_cost_net_usd: number | null;
    trades: number | null;
    profit_factor: number | null;
    max_drawdown_pct: number | null;
    sample_qualified: boolean;
  };
  latest_run: { symbol?: string; timeframe?: string; data_provenance?: string } | null;
  can_trade: false;
  can_promote: false;
}

export interface StrategyWorkflowPayload {
  workflow_id: string;
  generated_at?: string;
  status?: string;
  summary: {
    revisions?: number;
    explicit_revisions?: number;
    strategies?: number;
    quarantined?: number;
    shadow_observe?: number;
    oos_pass?: number;
    by_stage?: Record<string, number>;
  };
  revisions: StrategyWorkflowRevision[];
  policy: {
    immutable_revisions?: boolean;
    fork_requires_new_registered_strategy_id?: boolean;
    engine_parity_failure_quarantines_revision?: boolean;
    can_trade: false;
    can_promote: false;
  };
  can_trade: false;
  can_promote: false;
}

export interface MlStage {
  key: string;
  label: string;
  done: boolean;
  active?: boolean;
  detail?: string;
}

export interface MlStatus {
  artifact_available?: boolean;
  generated_at?: string;
  active_role?: string;
  stage: string;
  stages: MlStage[];
  dataset: {
    samples: number;
    win_rate_pct?: number;
    min_to_train: number;
    progress_pct: number;
    by_strategy: Record<string, number>;
  };
  foundation: {
    feature_count?: number;
    [k: string]: unknown;
  };
  gates: Record<string, unknown>;
  online_shadow?: {
    library: "river";
    installed: boolean;
    configured: boolean;
    active: boolean;
    role: string;
    min_resolved_labels: number;
    binding: false;
    can_trade: false;
    auto_retrain_live: false;
    drift_supervisor?: {
      policies_registered: boolean;
      configured_streams: number;
      detectors: string[];
      classes: string[];
      event_route: string;
      automatic_action: "none";
    };
    note: string;
  };
  model: Record<string, unknown> | null;
  can_trade: false;
  can_promote: false;
  note?: string;
  artifact?: ArtifactMetadata;
}

export interface AgenticResearchStatus {
  artifact_available?: boolean;
  os_id: string;
  generated_at?: string;
  mode?: string;
  summary: {
    operator_actions?: number;
    critical_actions?: number;
    warning_actions?: number;
    agent_health_min?: number;
    gateway_active_tasks?: number;
    [k: string]: unknown;
  };
  source_status: Array<{
    source?: string;
    state?: string;
    age_minutes?: number;
    generated_at?: string;
  }>;
  operator_answer?: string;
  can_trade: false;
  can_promote: false;
  live_orders_enabled: false;
  artifact?: ArtifactMetadata;
}

export interface PulseHour {
  symbol: string;
  open_time: string;
  open_time_utc: string;
  close_time: string;
  close_time_utc: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  vwap: number | null;
  session_vwap: number | null;
  range_bps: number;
  body_bps: number;
  close_vs_open_bps: number;
  volume_vs_median_20h: number | null;
  volume_vs_median_24h: number | null;
  volume_rank_24h: number | null;
  vs_session_vwap_bps: number | null;
  prior_hour_range_bps: number | null;
  dual_avwap_bias: string;
  avwap_low: number | null;
  avwap_high: number | null;
  avwap_low_anchor_utc: string | null;
  avwap_high_anchor_utc: string | null;
  avwap_low_confirmed_at_utc: string | null;
  avwap_high_confirmed_at_utc: string | null;
  session_active: boolean;
  session_label: string;
  data_quality: string;
  gap_minutes: number;
  stream_healthy: boolean;
  forming: boolean;
  is_gap: boolean;
}

export interface PulseForming {
  open_time: string;
  open_time_utc: string;
  mid: number | null;
  range_bps: number | null;
  body_bps: number | null;
  volume: number | null;
  volume_rank_24h: number | null;
  vs_session_vwap_bps: number | null;
  dual_avwap_bias: string;
  dual_avwap_reason: string | null;
  avwap_low: number | null;
  avwap_high: number | null;
  prior_day_poc: number | null;
  vs_prior_day_poc_bps: number | null;
  prior_day_value_area_location: string;
  session_label: string;
  session_active: boolean;
  status: "forming" | "awaiting_trades";
  data_quality: string;
  [k: string]: unknown;
}

export interface PulseAlert {
  kind: string;
  at: string;
  severity: "info" | "warning" | "critical";
  message: string;
  recovered: boolean;
}

export interface PulseRegimeContext {
  as_of_utc: string | null;
  symbol: string;
  timeframe: string;
  label: string;
  trend_direction: "up" | "down" | "flat";
  adx: number | null;
  atr_percentile: number | null;
  ema_slope_bps: number | null;
  bb_width_bps: number | null;
  bb_width_percentile: number | null;
  volume_ratio: number | null;
  confidence: number;
  data_quality: string;
  ready: boolean;
  reason: string;
  measurement_only: true;
}

export interface PulsePayload {
  exchange: string;
  symbol: string;
  as_of: string;
  as_of_utc: string;
  status: string;
  data_quality: string;
  quality_reason: string | null;
  forming: PulseForming;
  hours: PulseHour[];
  fee_wall_bps: number;
  session_vwap_series: Array<{ time: number; value: number }>;
  avwap_series: Array<{ time: number; value: number }> | null;
  dual_avwap_series: {
    low: Array<{ time: number; value: number }>;
    high: Array<{ time: number; value: number }>;
  };
  volume_profile: {
    prior_day: {
      available: boolean;
      source: "trades";
      source_exchange: string | null;
      window: "prior_utc_day";
      window_id: string;
      start: string;
      end: string;
      bin_size?: number;
      poc: number | null;
      val?: number;
      vah?: number;
      value_area_low: number | null;
      value_area_high: number | null;
      value_area_fraction?: number;
      target_pct?: number;
      va_volume_pct?: number;
      total_volume?: number;
      trade_count?: number;
      reference_price?: number | null;
      vs_poc_bps: number | null;
      location: string;
      reason?: string;
    };
  };
  regime: {
    "1h": PulseRegimeContext;
    "4h": PulseRegimeContext;
  };
  market: {
    last: number | null;
    mid: number | null;
    feed_age_ms: number | null;
    canonical_age_ms: number | null;
    canonical_state: "current" | "missing_expected_close" | "stale" | "missing";
    latest_close_utc: string | null;
    expected_close_utc: string;
    session_label: string;
    regime_1h: string;
    regime_4h: string;
  };
  indicators: {
    regime_1h: string;
    regime_4h: string;
    session_vwap: number | null;
    vs_session_vwap_bps: number | null;
    dual_avwap_bias: string;
    avwap: number | null;
    avwap_label: string | null;
    avwap_low: number | null;
    avwap_high: number | null;
    avwap_low_anchor_utc: string | null;
    avwap_high_anchor_utc: string | null;
    avwap_low_confirmed_at_utc: string | null;
    avwap_high_confirmed_at_utc: string | null;
    avwap_unavailable_reason: string | null;
    prior_day_poc: number | null;
    prior_day_value_area_low: number | null;
    prior_day_value_area_high: number | null;
    vs_prior_day_poc_bps: number | null;
    prior_day_value_area_location: string;
    volume_profile_unavailable_reason: string | null;
  };
  last_gap: {
    kind: string;
    start: string;
    end: string;
    recovered: boolean;
    detail: string;
  } | null;
  book: PriceBook | null;
  alerts: PulseAlert[];
  policy: string;
  read_only: boolean;
  can_trade: false;
}

export interface HourBrief {
  schema_version: "1.0";
  brief_id: string;
  exchange: string;
  symbol: string;
  hour_open_utc: string;
  hour_close_utc: string;
  generated_at_utc: string;
  data_quality: "ok" | "degraded" | "gap";
  inputs: Record<string, unknown>;
  sections: {
    state: { label: string; summary: string };
    what_mattered: { bullets: string[] };
    structure: { summary: string; bias_tag: string };
    risks: { bullets: string[] };
    watch_next: { summary: string };
  };
  flags: {
    feed_degraded: boolean;
    high_volume: boolean;
    wide_range: boolean;
    above_vwap: boolean;
  };
  disclaimer: string;
  model: string;
}
