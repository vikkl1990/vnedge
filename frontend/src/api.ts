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
