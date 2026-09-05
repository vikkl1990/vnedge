from __future__ import annotations

import pandas as pd

from vnedge.strategy.htf_regime_continuation_15m import (
    HtfRegimeContinuation15mV1,
)
from vnedge.strategy.htf_regime_continuation_15m_v2 import (
    HtfRegimeContinuation15mV2,
)
from vnedge.strategy.market_regime import MarketRegime
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


def test_regime_v2_is_a_separate_non_capital_ohlc_contract() -> None:
    strategy_id = HtfRegimeContinuation15mV2.strategy_id
    contract = scanner_runtime_contract(strategy_id)

    assert get_strategy_class(strategy_id) is HtfRegimeContinuation15mV2
    assert contract is not None
    assert contract.entry_clock == "next_open"
    assert contract.context_timeframes == ("4h", "1d")
    assert HtfRegimeContinuation15mV2.market_regime_config.weekly_classifier == (
        "range_structure_v1"
    )
    assert not is_capital_eligible(strategy_id)
    assert is_shadow_observe_eligible(strategy_id)


def test_regime_v2_price_only_structure_is_explicit_and_v1_remains_strict() -> None:
    v1 = HtfRegimeContinuation15mV1()
    v2 = HtfRegimeContinuation15mV2()

    assert v1._structure.allow_price_only_context is False
    assert v1._structure._hourly.allow_price_only_live is False
    assert v2._structure.allow_price_only_context is True
    assert v2._structure._hourly.allow_price_only_live is True


def test_regime_v2_does_not_apply_a_second_four_hour_bos_veto(monkeypatch) -> None:
    base = pd.DataFrame(
        [
            {
                "bos15_structure_ready": 1.0,
                "bos15_quality_ok": 1.0,
                "bos15_structure_trend": "down",
                # V2 uses the OHLC-only weekly classifier. A stale legacy
                # AVWAP column must not add a second, unavailable-data veto.
                "bos15_dual_avwap_bias": "strong_long",
                "hsc_volume_ratio": 2.0,
                "hsc_body_bps": 25.0,
                "hsc_pullback_long": 0.0,
                "hsc_pullback_short": 1.0,
                "hsc_projected_net_long_bps": 40.0,
                "hsc_projected_net_short_bps": 40.0,
                "hsc_htf_aligned_short": 0.0,
                "mreg_allow_long": 0.0,
                "mreg_allow_short": 1.0,
            }
        ]
    )
    monkeypatch.setattr(
        HtfRegimeContinuation15mV1,
        "prepare",
        lambda self, candles: base.copy(),
    )

    prepared = HtfRegimeContinuation15mV2().prepare(pd.DataFrame())

    assert prepared.iloc[0]["rt_allow_short"] == 1.0
    assert prepared.iloc[0]["rt_arm_ready"] == 1.0
    assert prepared.iloc[0]["mreg_structure_source"] == ("canonical_ohlc_price_only_v1")


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


def test_regime_cache_identity_changes_only_when_closed_context_advances() -> None:
    strategy = HtfRegimeContinuation15mV1()
    h4 = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-08-30T08:00:00Z", "2026-08-30T12:00:00Z"]),
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
        }
    )
    daily = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-08-28T00:00:00Z", "2026-08-29T00:00:00Z"]),
            "open": [98.0, 99.0],
            "high": [102.0, 103.0],
            "low": [97.0, 98.0],
            "close": [99.0, 102.0],
        }
    )
    strategy.bind_canonical_context("4h", h4)
    strategy.bind_canonical_context("1d", daily)

    before = strategy._context_identity(pd.Timestamp("2026-08-30T12:15:00Z"))
    same_context = strategy._context_identity(pd.Timestamp("2026-08-30T12:30:00Z"))
    after = strategy._context_identity(pd.Timestamp("2026-08-30T16:15:00Z"))

    assert before == same_context
    assert after != before


def test_prepare_reads_cached_weekly_artifact_once_per_htf_identity(monkeypatch) -> None:
    strategy = HtfRegimeContinuation15mV1()
    timestamps = pd.date_range("2026-08-30T12:00:00Z", periods=4, freq="15min")
    prepared = pd.DataFrame(
        {
            "timestamp": timestamps,
            "rt_allow_long": 1.0,
            "rt_allow_short": 0.0,
            "rt_arm_ready": 1.0,
        }
    )
    monkeypatch.setattr(
        "vnedge.strategy.realtime_scanners.HtfStructureContinuationRealtimeV1.prepare",
        lambda self, candles: prepared.copy(),
    )
    h4 = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-08-30T08:00:00Z"]),
            "open": [100.0],
            "high": [102.0],
            "low": [99.0],
            "close": [101.0],
        }
    )
    daily = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-08-29T00:00:00Z"]),
            "open": [99.0],
            "high": [103.0],
            "low": [98.0],
            "close": [102.0],
        }
    )
    strategy.bind_canonical_context("4h", h4)
    strategy.bind_canonical_context("1d", daily)
    strategy.bind_weekly_vwap_artifacts(
        pd.DataFrame(
            [
                {
                    "exchange": "delta_india",
                    "symbol": "BTCUSD",
                    "timeframe": "1w",
                    "open_time": pd.Timestamp("2026-08-17T00:00:00Z"),
                    "close_time": pd.Timestamp("2026-08-24T00:00:00Z"),
                    "vwap": 100.0,
                    "sum_base": 1.0,
                    "sum_notional": 100.0,
                    "n_trades": 10,
                    "source": "trade_lake",
                    "coverage_ok": True,
                }
            ]
        )
    )
    calls = 0

    def regime_at(decision_at, *, machine=None):
        nonlocal calls
        calls += 1
        return MarketRegime(
            weekly="up",
            daily="mid",
            h4="up",
            allow_long=True,
            allow_short=False,
            state="continuation",
            reason="test",
            ema_state="up",
            macd_impulse="on",
        )

    monkeypatch.setattr(strategy, "_regime_at", regime_at)

    strategy.prepare(pd.DataFrame())

    assert calls == 1


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


def test_next_open_signal_carries_actual_bound_permission_snapshot() -> None:
    strategy = HtfRegimeContinuation15mV2()
    strategy.warmup_bars = 0
    h4 = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-09-04T04:00:00Z"),
                "open": 98.0,
                "high": 103.0,
                "low": 97.0,
                "close": 101.0,
                "volume": 80.0,
                "is_closed": True,
                "data_quality": "ok",
                "candle_source": "canonical_tick_lake",
            }
        ]
    )
    daily = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-09-03T00:00:00Z"),
                "open": 95.0,
                "high": 104.0,
                "low": 94.0,
                "close": 101.0,
                "volume": 400.0,
                "is_closed": True,
                "data_quality": "ok",
                "candle_source": "router",
            }
        ]
    )
    strategy.bind_canonical_context("4h", h4)
    strategy.bind_canonical_context("1d", daily)
    prepared = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-09-04T12:00:00Z"),
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 10.0,
                "is_closed": True,
                "data_quality": "ok",
                "candle_source": "router",
                "rt_allow_long": 1.0,
                "rt_allow_short": 0.0,
                "rt_long_structural_stop": 98.0,
                "mreg_weekly": "up",
                "mreg_daily": "mid",
                "mreg_h4": "up",
                "mreg_ema_state": "up",
                "mreg_macd_impulse": "on",
                "mreg_rsi_zone": "mid",
                "mreg_reason": "aligned",
            }
        ]
    )

    intent = strategy.signal(prepared, 0)

    assert intent is not None
    assert intent.permission_snapshot is not None
    assert intent.permission_snapshot.context_bars[0].open_time == pd.Timestamp(
        "2026-09-04T04:00:00Z"
    )
    assert intent.permission_snapshot.context_bars[1].source == "router"

    # Losing the bound daily row after feature preparation must not leave a
    # floor-derived permission behind. The production signal boundary checks
    # the actual context store again before constructing an intent.
    strategy.bind_canonical_context("1d", pd.DataFrame())
    assert strategy.signal(prepared, 0) is None
    diagnostics = strategy.evaluation_diagnostics(prepared, 0)
    assert diagnostics["primary_failed_gate"] == "htf_context_missing"
    assert diagnostics["eligible"] is False
