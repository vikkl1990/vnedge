"""Runner configuration for legacy observe/simulated-execution loops.

``SHADOW`` maps to the canonical OBSERVE stage and ``PAPER`` maps to the
canonical SHADOW execution stage.  Keep the enum values for configuration
compatibility; runtime authority comes from ``ExecutionContext``.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from vnedge.config.risk_config import RiskConfig
from vnedge.risk.position_sizer import SymbolLimits
from vnedge.risk.protections import ProtectionConfig
from vnedge.runtime.daily_factory import DailySignalFactoryConfig


class RunnerMode(str, Enum):
    PAPER = "paper"
    SHADOW = "shadow"


class EntryRoute(str, Enum):
    """How an approved signal reaches the execution boundary.

    ``AUTO`` preserves historical strategy-prefix routing while old manifests
    are replayed.  New observer manifests must state taker vs maker-retest
    explicitly so shadow, paper, and a future live adapter see one contract.
    """

    AUTO = "auto"
    TAKER = "taker"
    MAKER_RETEST = "maker_retest"


class RunnerConfig(BaseModel):
    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    mode: RunnerMode = RunnerMode.SHADOW  # safe default, like everything here
    symbol: str = "BTC/USDT:USDT"
    timeframe: str = "1h"
    starting_equity_usd: float = Field(default=500.0, gt=0)
    spread_bps: float = Field(default=1.0, ge=0)
    slippage_est_bps: float = Field(default=2.0, ge=0)
    max_holding_bars: int = Field(default=48, ge=1)
    reconcile_every_bars: int = Field(default=24, ge=1)
    # Dynamic ATR-chandelier trailing on the active-exit remainder (DEFAULT OFF).
    # Same engine + canonical ATR the backtester uses, so research and runtime
    # trail identically. 0.0 = legacy arm-and-lock. Only enable in BOTH backtest
    # and runtime together, or the two diverge.
    trail_atr_mult: float = Field(default=0.0, ge=0.0)
    trail_atr_window: int = Field(default=14, ge=1)
    allow_partial_tp: bool = True
    fee_aware_breakeven_bps: float = Field(default=8.0, ge=0.0)
    # LEGACY ALIAS (PR #92) — maps into protections.cooldown_bars_after_stop
    # via effective_protections(). Semantics were refined when the protections
    # state machine landed: the cooldown now arms on STOP exits only (a winner
    # closing is not evidence the entry condition went bad).
    # DEFAULT OFF: enabling this changes entry behavior — running trials are
    # frozen, so it may only be turned on via a pre-registered future protocol.
    post_exit_cooldown_bars: int = Field(default=0, ge=0)
    # Entry protections (risk/protections.py): post-stop cooldown and the
    # stop-window guard. ALL DEFAULT OFF; enabling any of them on a trial
    # requires pre-registration (docs/PROTECTIONS.md). Exits are never
    # affected — the state machine has no exit-blocking path at all.
    protections: ProtectionConfig = Field(default_factory=ProtectionConfig)
    # Daily signal-factory discipline. Default OFF so existing judged trials
    # keep their exact behavior; enable per lane/profile when the operating
    # contract is intraday-only: no late entries, force-flat before session
    # close, and stop after the daily target is banked.
    daily_factory: DailySignalFactoryConfig = Field(
        default_factory=DailySignalFactoryConfig
    )
    # Tick-granular STOP monitoring: between bar closes, the idle loop checks
    # the live top-of-book against the open plan's stop and exits reduce-only
    # on breach. STOPS ONLY — a stop is capital protection, so it gets the
    # finest granularity available; take-profits stay bar-close because TP
    # timing is strategy semantics that the backtester models at bar
    # granularity (tick-level TPs would make paper diverge from research).
    tick_stops_enabled: bool = True
    # A venue close notification can beat the trade-derived candle writer by a
    # few hundred milliseconds. Scanner decisions wait for the matching
    # canonical row instead of evaluating an exchange-OHLCV substitute and
    # repairing it one bar too late. Zero keeps unit/offline sessions instant;
    # production multi-lane config supplies the bounded wait explicitly.
    canonical_candle_wait_seconds: float = Field(default=0.0, ge=0.0, le=30.0)
    canonical_candle_poll_seconds: float = Field(default=0.20, gt=0.0, le=5.0)
    # Runtime feature frames are working state, not the historical lake.  Keep
    # them bounded so per-close preparation cost and memory stay flat with
    # process uptime.  The session raises this floor automatically when a
    # strategy warmup/hold/trailing contract needs more rows.
    # Keep only a small generic floor. ``LivePaperSession`` raises this to the
    # strategy's declared warmup + holding/exit buffer, so Range still retains
    # its complete 20-day hour profile while 15m structure lanes no longer
    # rescan 4,096 rows on every close.  The former 4,096 default made eight
    # concurrent scanners spend 5-30 seconds in pandas and starved BBO/API
    # handling even though their frozen contracts needed only a few hundred
    # rows.
    working_frame_bars: int = Field(default=256, ge=256, le=100_000)
    # The public market-data venue and the assumed execution-cost venue are
    # different contracts.  Leave this unset only when they are intentionally
    # the same; shadow deployments that model Delta fees while reading Binance
    # must name ``delta_india`` explicitly.
    execution_cost_exchange_id: str | None = None
    # Entry routing is execution policy, not signal logic.  Keeping it on the
    # runner makes one scanner cohort comparable across shadow/paper/live.
    entry_route: EntryRoute = EntryRoute.AUTO
    maker_fill_ttl_bars: int = Field(default=1, ge=1, le=288)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    limits: SymbolLimits = Field(
        default=SymbolLimits(
            min_qty=0.0001, qty_step=0.0001, min_notional_usd=5.0,
            maintenance_margin_rate=0.005,
        )
    )

    def effective_protections(self) -> ProtectionConfig:
        """Protections with the legacy post_exit_cooldown_bars alias folded in.

        The stricter of the two cooldown values wins, so a config that sets
        either field keeps its protection.
        """
        if self.post_exit_cooldown_bars <= self.protections.cooldown_bars_after_stop:
            return self.protections
        return self.protections.model_copy(
            update={"cooldown_bars_after_stop": self.post_exit_cooldown_bars}
        )
