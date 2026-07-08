"""Runner configuration for paper/shadow loops."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from vnedge.config.risk_config import RiskConfig
from vnedge.risk.position_sizer import SymbolLimits


class RunnerMode(str, Enum):
    PAPER = "paper"
    SHADOW = "shadow"


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
    # Tick-granular STOP monitoring: between bar closes, the idle loop checks
    # the live top-of-book against the open plan's stop and exits reduce-only
    # on breach. STOPS ONLY — a stop is capital protection, so it gets the
    # finest granularity available; take-profits stay bar-close because TP
    # timing is strategy semantics that the backtester models at bar
    # granularity (tick-level TPs would make paper diverge from research).
    tick_stops_enabled: bool = True
    risk: RiskConfig = Field(default_factory=RiskConfig)
    limits: SymbolLimits = Field(
        default=SymbolLimits(
            min_qty=0.0001, qty_step=0.0001, min_notional_usd=5.0,
            maintenance_margin_rate=0.005,
        )
    )
