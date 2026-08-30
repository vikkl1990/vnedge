from __future__ import annotations

import pandas as pd

from vnedge.strategy.htf_regime_continuation_15m import (
    HtfRegimeContinuation15mV1,
)
from vnedge.strategy.scanner_contracts import scanner_runtime_contract
from vnedge.strategy.strategy_registry import (
    get_strategy_class,
    is_capital_eligible,
    is_shadow_observe_eligible,
)


def test_regime_strategy_is_next_open_research_only() -> None:
    strategy_id = HtfRegimeContinuation15mV1.strategy_id
    contract = scanner_runtime_contract(strategy_id)

    assert get_strategy_class(strategy_id) is HtfRegimeContinuation15mV1
    assert contract is not None
    assert contract.entry_clock == "next_open"
    assert contract.context_timeframes == ("4h", "1d")
    assert not is_capital_eligible(strategy_id)
    assert not is_shadow_observe_eligible(strategy_id)


def test_regime_strategy_cannot_create_a_quote_arm() -> None:
    strategy = HtfRegimeContinuation15mV1()

    assert strategy.realtime_arm(pd.DataFrame(), 0) is None


def test_unhealthy_regime_context_fails_closed() -> None:
    strategy = HtfRegimeContinuation15mV1()
    decision_at = pd.Timestamp("2026-08-30T12:15:00Z")

    regime = strategy._regime_at(decision_at)

    assert not regime.ready
    assert regime.state == "flat"
    assert regime.family == "flat"
    assert not regime.allow_long
    assert not regime.allow_short
    assert regime.reason == "data_unhealthy:canonical_regime_context_unhealthy"
    assert regime.exit_reason == "htf_bias_invalidated"


def test_premium_blocks_new_entry_but_does_not_exit_aligned_continuation() -> None:
    strategy = HtfRegimeContinuation15mV1()
    prepared = pd.DataFrame(
        [
            {
                "close": 100.0,
                "mreg_state": "continuation",
                "mreg_weekly": "up",
                "mreg_ema_state": "up",
                "mreg_h4": "up",
                "mreg_allow_long": 0.0,
                "mreg_rsi_zone": "premium",
            }
        ]
    )

    assert strategy.exit_signal(prepared, 0, "long", 95.0) is None


def test_regime_invalidation_requests_reduce_only_exit() -> None:
    strategy = HtfRegimeContinuation15mV1()
    prepared = pd.DataFrame(
        [
            {
                "close": 99.0,
                "mreg_state": "flat",
                "mreg_weekly": "up",
                "mreg_ema_state": "up",
                "mreg_h4": "down",
                "mreg_exit_reason": "htf_bias_invalidated",
            }
        ]
    )

    intent = strategy.exit_signal(prepared, 0, "long", 100.0)

    assert intent is not None
    assert intent.reason == "htf_bias_invalidated"
    assert intent.exit_price == 99.0
