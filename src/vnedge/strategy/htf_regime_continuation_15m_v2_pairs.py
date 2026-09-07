"""Symbol-scoped registrations over the frozen HTF continuation V2 engine.

The market claim and feature code remain owned by
``HtfRegimeContinuation15mV2``.  These classes only freeze the pair identity
so BTC and ETH can never share a decision stream, cost profile, or scoreboard.
The unscoped V2 registration remains available for historical evidence but is
not used by the checked-in observer roster.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from vnedge.strategy.htf_regime_continuation_15m_v2 import (
    STRATEGY_SPEC as BASE_STRATEGY_SPEC,
)
from vnedge.strategy.htf_regime_continuation_15m_v2 import (
    HtfRegimeContinuation15mV2,
)

BTC_STRATEGY_ID: Final = "htf_regime_continuation_15m_v2__BTCUSD"
ETH_STRATEGY_ID: Final = "htf_regime_continuation_15m_v2__ETHUSD"


def _pair_spec(
    strategy_id: str,
    *,
    symbol: str,
    cost_profile_id: str,
) -> MappingProxyType:
    return MappingProxyType(
        {
            **dict(BASE_STRATEGY_SPEC),
            "strategy_id": strategy_id,
            "symbol": symbol,
            "symbols": (symbol,),
            "cost_profile_id": cost_profile_id,
            "parent_strategy_id": HtfRegimeContinuation15mV2.strategy_id,
        }
    )


BTC_STRATEGY_SPEC = _pair_spec(
    BTC_STRATEGY_ID,
    symbol="BTCUSD",
    cost_profile_id="delta_swing_btc_v1",
)
ETH_STRATEGY_SPEC = _pair_spec(
    ETH_STRATEGY_ID,
    symbol="ETHUSD",
    cost_profile_id="delta_swing_eth_v1",
)


class HtfRegimeContinuation15mV2BTCUSD(HtfRegimeContinuation15mV2):
    """BTCUSD-only registration using the frozen V2 continuation engine."""

    strategy_id = BTC_STRATEGY_ID
    allowed_data_symbols = ("BTCUSD",)
    cost_profile_id = "delta_swing_btc_v1"


class HtfRegimeContinuation15mV2ETHUSD(HtfRegimeContinuation15mV2):
    """ETHUSD-only registration using the frozen V2 continuation engine."""

    strategy_id = ETH_STRATEGY_ID
    allowed_data_symbols = ("ETHUSD",)
    cost_profile_id = "delta_swing_eth_v1"


PAIR_STRATEGIES = (
    HtfRegimeContinuation15mV2BTCUSD,
    HtfRegimeContinuation15mV2ETHUSD,
)

PAIR_STRATEGY_SPECS = (BTC_STRATEGY_SPEC, ETH_STRATEGY_SPEC)

__all__ = [
    "BTC_STRATEGY_ID",
    "BTC_STRATEGY_SPEC",
    "ETH_STRATEGY_ID",
    "ETH_STRATEGY_SPEC",
    "PAIR_STRATEGIES",
    "PAIR_STRATEGY_SPECS",
    "HtfRegimeContinuation15mV2BTCUSD",
    "HtfRegimeContinuation15mV2ETHUSD",
]
