// Browser authentication never places a credential in the URL. The root
// token is submitted once in an Authorization header and exchanged for a
// short-lived HttpOnly cookie; all later HTTP/WebSocket calls use that cookie.

export interface BrowserSession {
  expires_at: string | null;
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
  symbol: string;
  timeframe: string;
  capital: boolean;
  venue_rtt_ms: number | null;
  candle_status: string;
  candle_age_ms: number | null;
  bar_close_processing_ms: number | null;
  decision_lag_ms: number | null;
  latency_samples: { bar_close: number; decision: number; required: number };
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
  active_plan: Record<string, unknown> | null;
  last_eval: Record<string, unknown> | null;
  why_no_fire: string;
  last_reject_reason: string | null;
  shadow_perf: {
    pending_shadow_intents?: number;
    shadow_outcomes_recent?: Record<string, unknown>[];
    virtual_net_usd?: number;
    wins?: number;
    losses?: number;
    profit_factor?: number | null;
    bars_since_signal?: number | null;
  } | null;
}

export interface LanesPayload {
  lanes: CorrectionLane[];
  capital_roster_size: number;
  measurement_only: boolean;
  banner: string | null;
  shadow_observe_lanes: number;
  portfolio: PortfolioScope;
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

export interface JournalPayload {
  generated_at: string;
  summary: {
    positions: number;
    open_orders: number;
    fills: number;
    closed_trades: number;
    actual_realized_pnl_usd: number;
    fees_usd: number;
    virtual_net_usd: number;
    [k: string]: unknown;
  };
  closed_trades: JournalRow[];
  events: Array<{
    lane?: string;
    ts?: string;
    event?: string;
    detail?: string;
    [k: string]: unknown;
  }>;
  orders: Array<Record<string, unknown>>;
  fills: Array<Record<string, unknown>>;
}

export interface MetaPayload {
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
