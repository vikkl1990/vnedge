// Thin fetch client over the existing token-gated dashboard endpoints.
// The token is read from the ?token= query param (same convention as the
// classic dashboard) and sent as a Bearer header — never placed in logs.

export function getToken(): string {
  return new URLSearchParams(window.location.search).get("token") ?? "";
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (res.status === 401) throw new ApiError(401, "unauthorized — check the token");
  if (!res.ok) throw new ApiError(res.status, `request failed (${res.status})`);
  return (await res.json()) as T;
}

// --- Response shapes (only the fields the panels read) ---------------------
export interface WhoAmI {
  name: string | null;
  role: string | null;
  permissions: string[];
}

export interface Position {
  symbol?: string;
  side?: string;
  quantity?: number;
  entry_price?: number;
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
  p50?: number;
  p95?: number;
  n?: number;
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
  decision_skips?: Record<string, number> | null;
  regime?: RegimeReading | null;
  plan_overlay?: PlanOverlay | null;
  equity?: number;
  peak_equity?: number;
  drawdown_pct?: number | null;
  dd_limit_pct?: number | null;
  trial_scorecard?: TrialScorecard | null;
  bands?: { age?: string; decision_lag?: string; dd?: string; verdict_tone?: string } | null;
}

export interface CorrectionLane {
  lane_id: string;
  strategy_id: string;
  eligibility: "eligible" | "KILLED" | "RESEARCH_ONLY" | "unknown";
  mode: "shadow" | "paper" | "measurement" | "off";
  exchange: string;
  symbol: string;
  timeframe: string;
  capital: boolean;
  last_signal_age_seconds: number | null;
  health: "ok" | "degraded" | "unknown";
}

export interface LanesPayload {
  lanes: CorrectionLane[];
  capital_roster_size: number;
  measurement_only: boolean;
  banner: string | null;
  read_only: true;
  can_promote: false;
  can_trade: false;
}

export interface RiskSnapshot {
  runtime_mode: "measurement" | "shadow" | "paper" | "live_blocked" | string;
  runtime_label: string;
  capital: { enabled: boolean; roster_size: number };
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
  gateway: { last_reject_reasons: { reason: string; count: number }[] };
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
  exit_reason?: string;
  [k: string]: unknown;
}

export interface PulseHour {
  symbol: string;
  open_time: string;
  close_time: string;
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
  session_active: boolean;
  session_label: string;
  data_quality: string;
  gap_minutes: number;
  stream_healthy: boolean;
  forming: boolean;
}

export interface PulseAlert {
  kind: string;
  at: string;
  severity: "info" | "warning" | "critical";
  message: string;
  recovered: boolean;
}

export interface PulsePayload {
  exchange: string;
  symbol: string;
  as_of: string;
  status: string;
  data_quality: string;
  forming: Record<string, unknown> | null;
  hours: PulseHour[];
  indicators: {
    session_vwap: number | null;
    vs_session_vwap_bps: number | null;
    dual_avwap_bias: string;
    avwap: number | null;
    avwap_label: string | null;
  };
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
