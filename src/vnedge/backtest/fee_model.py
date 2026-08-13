"""Trading fee model.

Defaults match Binance USDT-M futures standard tier (maker 2 bps, taker 5
bps). The v1 backtester enters and exits with market orders, so the taker
rate applies on both sides; the maker rate exists for future limit-order
support.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from vnedge.plan.cost_model import DEFAULT_MAKER_FEE_BPS, DEFAULT_TAKER_FEE_BPS


class FeeModel(BaseModel):
    model_config = {"frozen": True}

    # defaults sourced from plan.cost_model — one fee assumption across research/paper/live
    maker_bps: float = Field(default=DEFAULT_MAKER_FEE_BPS, ge=0)
    taker_bps: float = Field(default=DEFAULT_TAKER_FEE_BPS, ge=0)

    def taker_fee_usd(self, notional_usd: float) -> float:
        return abs(notional_usd) * self.taker_bps / 10_000.0

    def maker_fee_usd(self, notional_usd: float) -> float:
        return abs(notional_usd) * self.maker_bps / 10_000.0
