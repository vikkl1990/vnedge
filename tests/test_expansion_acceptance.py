"""V3 lifecycle tests: re-arm, two-sided independence, quote acceptance."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.runtime.expansion_acceptance import (
    AcceptanceState,
    CompressionArm,
    ExpansionAcceptanceEngine,
)
from vnedge.runtime.squeeze_acceptance_observe import SqueezeAcceptanceObserveRunner
from vnedge.strategy.squeeze_expansion_breakout_v3 import SqueezeExpansionV3Params


def _config(**overrides) -> SqueezeExpansionV3Params:
    values = {
        "arm_grace_bars": 3,
        "acceptance_hold_seconds": 5.0,
        "min_acceptance_samples": 3,
        "max_chase_bps": 20.0,
        "max_probes_per_side": 3,
        "max_fires_per_side": 2,
        "cooldown_loss_bars": 0,
        "cooldown_win_bars": 0,
    }
    values.update(overrides)
    return SqueezeExpansionV3Params(**values)


def _armed(config: SqueezeExpansionV3Params | None = None) -> ExpansionAcceptanceEngine:
    engine = ExpansionAcceptanceEngine(config=config or _config())
    engine.update_arm(CompressionArm(
        episode_id=7, box_high=100.0, box_low=99.0, atr=0.25,
        vwap=99.5, bar_index=10, compressed=True,
    ))
    return engine


def test_failed_long_probe_rearms_without_burning_short() -> None:
    engine = _armed()
    t0 = datetime(2026, 8, 20, tzinfo=UTC)
    assert engine.observe_quote(bid=100.02, ask=100.05, ts=t0, bar_index=10) is None
    assert engine.long.state is AcceptanceState.PROBE
    assert engine.short.state is AcceptanceState.ARMED

    # Back inside the long break level: that probe failed, but both directions
    # remain usable for the later real move.
    assert engine.observe_quote(
        bid=99.97, ask=100.0, ts=t0 + timedelta(seconds=1), bar_index=10
    ) is None
    assert engine.long.state is AcceptanceState.ARMED
    assert engine.short.state is AcceptanceState.ARMED
    assert engine.long.probes == 1
    assert engine.hold_observation_id == 1
    assert engine.last_hold_ms == 1000.0


def test_quote_hold_accepts_at_current_ask_not_old_level() -> None:
    engine = _armed()
    t0 = datetime(2026, 8, 20, tzinfo=UTC)
    for seconds, ask in ((0, 100.05), (2, 100.07)):
        assert engine.observe_quote(
            bid=ask - 0.01, ask=ask, ts=t0 + timedelta(seconds=seconds),
            bar_index=10,
        ) is None
    fire = engine.observe_quote(
        bid=100.08, ask=100.09, ts=t0 + timedelta(seconds=5), bar_index=10
    )
    assert fire is not None
    assert fire.side == "long"
    assert fire.entry == 100.09
    assert fire.entry > fire.level
    assert engine.position_open
    assert engine.short.state is AcceptanceState.ARMED
    assert engine.hold_observation_id == 1
    assert engine.last_hold_ms == 5000.0


def test_loss_rearms_same_side_but_net_win_burns_it() -> None:
    engine = _armed()
    t0 = datetime(2026, 8, 20, tzinfo=UTC)
    for seconds in (0, 2, 5):
        fire = engine.observe_quote(
            bid=100.07, ask=100.08, ts=t0 + timedelta(seconds=seconds),
            bar_index=10,
        )
    assert fire is not None
    engine.notify_flat(bar_index=11, net_won=False)
    assert engine.long.state is AcceptanceState.ARMED
    assert engine.short.state is AcceptanceState.ARMED

    for seconds in (10, 12, 15):
        fire2 = engine.observe_quote(
            bid=100.07, ask=100.08, ts=t0 + timedelta(seconds=seconds),
            bar_index=11,
        )
    assert fire2 is not None
    engine.notify_flat(bar_index=12, net_won=True)
    assert engine.long.state is AcceptanceState.BURNED
    assert engine.short.state is AcceptanceState.ARMED


def test_arm_survives_grace_then_expires() -> None:
    engine = _armed()
    t0 = datetime(2026, 8, 20, tzinfo=UTC)
    # Compression ended, but the causal box remains armed for three bars.
    assert engine.observe_quote(bid=99.5, ask=99.6, ts=t0, bar_index=13) is None
    assert engine.arm is not None
    assert engine.observe_quote(
        bid=99.5, ask=99.6, ts=t0 + timedelta(seconds=1), bar_index=14
    ) is None
    assert engine.arm is None
    assert engine.long.state is AcceptanceState.DORMANT


def test_chase_burn_is_side_local() -> None:
    engine = _armed(_config(max_chase_bps=5.0))
    t0 = datetime(2026, 8, 20, tzinfo=UTC)
    assert engine.observe_quote(bid=100.09, ask=100.10, ts=t0, bar_index=10) is None
    assert engine.long.state is AcceptanceState.BURNED
    assert engine.short.state is AcceptanceState.ARMED


def test_duplicate_sequence_does_not_manufacture_acceptance_samples() -> None:
    engine = _armed()
    t0 = datetime(2026, 8, 20, tzinfo=UTC)
    assert engine.observe_quote(
        bid=100.07, ask=100.08, ts=t0, received_ts=t0,
        sequence=10, source="binance:book", exchange_timestamped=True,
        bar_index=10,
    ) is None
    assert engine.long.probe_samples == 1
    assert engine.observe_quote(
        bid=100.07, ask=100.08, ts=t0, received_ts=t0 + timedelta(seconds=2),
        sequence=10, source="binance:book", exchange_timestamped=True,
        bar_index=10,
    ) is None
    assert engine.last_reason == "quote_duplicate"
    assert engine.long.probe_samples == 1
    assert engine.observe_quote(
        bid=100.07, ask=100.08, ts=t0 + timedelta(seconds=2),
        received_ts=t0 + timedelta(seconds=2), sequence=11,
        source="binance:book", exchange_timestamped=True, bar_index=10,
    ) is None
    fire = engine.observe_quote(
        bid=100.07, ask=100.08, ts=t0 + timedelta(seconds=5),
        received_ts=t0 + timedelta(seconds=5), sequence=12,
        source="binance:book", exchange_timestamped=True, bar_index=10,
    )
    assert fire is not None


def test_stale_and_out_of_order_exchange_quotes_fail_closed() -> None:
    engine = _armed(_config(max_quote_lag_seconds=1.0))
    t0 = datetime(2026, 8, 20, tzinfo=UTC)
    assert engine.observe_quote(
        bid=100.07, ask=100.08, ts=t0,
        received_ts=t0 + timedelta(seconds=2), sequence=1,
        source="binance:book", exchange_timestamped=True, bar_index=10,
    ) is None
    assert engine.last_reason == "quote_ingest_lag"
    assert engine.long.probe_samples == 0
    assert engine.observe_quote(
        bid=100.07, ask=100.08, ts=t0 + timedelta(seconds=3),
        received_ts=t0 + timedelta(seconds=3), sequence=2,
        source="binance:book", exchange_timestamped=True, bar_index=10,
    ) is None
    assert engine.observe_quote(
        bid=100.07, ask=100.08, ts=t0 + timedelta(seconds=1),
        received_ts=t0 + timedelta(seconds=3), sequence=3,
        source="binance:book", exchange_timestamped=False, bar_index=10,
    ) is None
    assert engine.last_reason == "quote_out_of_order"


def test_future_exchange_quote_does_not_count_toward_hold() -> None:
    engine = _armed(_config(max_quote_future_skew_seconds=0.5))
    received = datetime(2026, 8, 20, tzinfo=UTC)

    assert engine.observe_quote(
        bid=100.07,
        ask=100.08,
        ts=received + timedelta(seconds=1),
        received_ts=received,
        sequence=1,
        source="binance:book",
        exchange_timestamped=True,
        bar_index=10,
    ) is None
    assert engine.last_reason == "quote_clock_skew"
    assert engine.long.probe_samples == 0


def test_quote_overflow_resets_probe_without_burning_arm() -> None:
    engine = _armed()
    t0 = datetime(2026, 8, 20, tzinfo=UTC)
    assert engine.observe_quote(
        bid=100.07,
        ask=100.08,
        ts=t0,
        bar_index=10,
    ) is None
    assert engine.long.state is AcceptanceState.PROBE
    assert engine.long.probe_samples == 1

    engine.note_quote_overflow(2)

    assert engine.last_reason == "quote_buffer_overflow"
    assert engine.quote_overflow_drops == 2
    assert engine.quote_contract_rejects == 2
    assert engine.quote_rearms == 1
    assert engine.overflow_probe_resets == 1
    assert engine.long.state is AcceptanceState.ARMED
    assert engine.long.probe_samples == 0

    # The missing interval cannot count toward the hold. Three new distinct
    # observations over the complete five seconds are required.
    for seconds in (1, 3):
        assert engine.observe_quote(
            bid=100.07,
            ask=100.08,
            ts=t0 + timedelta(seconds=seconds),
            bar_index=10,
        ) is None
    fire = engine.observe_quote(
        bid=100.07,
        ask=100.08,
        ts=t0 + timedelta(seconds=6),
        bar_index=10,
    )
    assert fire is not None


def test_full_round_trip_cost_controls_breakeven_ratchet() -> None:
    exits = ExitEngine(ExitConfig(
        breakeven_arm_r=1.0,
        trail_arm_r=2.0,
        breakeven_cost_bps=19.8,
        be_fee_buffer_bps=1.0,
    ))
    exits.open_from_fire(
        side="long", entry=100.0, stop=99.0, risk=1.0,
        box_edge=99.5, entry_bar=0,
    )
    assert exits.on_bar(high=101.1, low=100.0, close=101.0, atr=0.5, bar_index=1) is None
    assert exits.pos is not None
    assert exits.pos.stop == pytest.approx(100.208)


class _Journal:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def append(self, kind: str, payload: dict) -> None:
        self.records.append((kind, payload))


def test_shadow_runner_journals_quote_entry_and_after_cost_outcome() -> None:
    journal = _Journal()
    runner = SqueezeAcceptanceObserveRunner(journal=journal, symbol="BTC/USDT:USDT")
    bars = pd.DataFrame([
        {
            "timestamp": datetime(2026, 8, 20, tzinfo=UTC),
            "open": 99.5, "high": 100.0, "low": 99.0, "close": 99.8,
            "volume": 1000.0, "sqz_episode": 7.0,
            "sqz_range_high": 100.0, "sqz_range_low": 99.0,
            "sqz_atr": 0.25, "sqz_vwap24": 99.5, "sqz_compressed": 1.0,
        },
        {
            "timestamp": datetime(2026, 8, 20, 0, 5, tzinfo=UTC),
            "open": 100.0, "high": 100.1, "low": 99.5, "close": 99.7,
            "volume": 1200.0, "sqz_episode": 7.0,
            "sqz_range_high": 100.0, "sqz_range_low": 99.0,
            "sqz_atr": 0.25, "sqz_vwap24": 99.5, "sqz_compressed": 1.0,
        },
    ])
    runner.on_prepared_bar(bars, 0, bars.iloc[0]["timestamp"])
    t0 = datetime(2026, 8, 20, 0, 1, tzinfo=UTC)
    for seconds in (0, 2, 5):
        runner.on_quote(
            bid=100.08, ask=100.09, ts=t0 + timedelta(seconds=seconds)
        )
    assert runner.has_open
    intent = [payload for kind, payload in journal.records if kind == "shadow_intent"]
    assert len(intent) == 1
    assert intent[0]["entry_price"] == 100.09
    assert intent[0]["intent"]["strategy_id"] == "squeeze_expansion_breakout_v3"
    transitions = [
        payload for kind, payload in journal.records if kind == "scanner_transition"
    ]
    assert transitions
    assert transitions[-1]["state"] == "long_accepted"
    assert transitions[-1]["quotes_distinct"] == 3

    open_stats = runner.stats()
    assert open_stats["open_intents"] == 1
    assert open_stats["open_position"]["mark_basis"] == "executable_bid"
    assert open_stats["open_unrealized_net_usd"] < 0
    assert open_stats["total_net_usd"] == open_stats["open_unrealized_net_usd"]

    runner.on_prepared_bar(bars, 1, bars.iloc[1]["timestamp"])
    outcomes = [payload for kind, payload in journal.records if kind == "shadow_outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["net_won"] is False
    assert outcomes[0]["net_bps"] < outcomes[0]["captured_bps"]
    assert outcomes[0]["gross_pnl_usd"] == pytest.approx(
        outcomes[0]["captured_bps"] * runner.notional_usd / 10_000
    )
    assert outcomes[0]["entry_ts"] == (t0 + timedelta(seconds=5)).isoformat()
    assert outcomes[0]["cost_profile"] == "delta_scalp"
    assert outcomes[0]["cost_contract_version"] == "scanner_cost_v1"
    assert outcomes[0]["funding_complete"] is True
    assert "mfe_bps" in outcomes[0] and "mae_bps" in outcomes[0]
    assert runner.acceptance.long.state is AcceptanceState.ARMED


def test_runner_ignores_pre_entry_low_in_first_closed_candle() -> None:
    journal = _Journal()
    runner = SqueezeAcceptanceObserveRunner(journal=journal, symbol="ETH/USDT:USDT")
    bars = pd.DataFrame(
        [
            {
                "timestamp": datetime(2026, 8, 20, 0, 40, tzinfo=UTC),
                "open": 99.5, "high": 100.0, "low": 99.0, "close": 99.8,
                "volume": 1000.0, "sqz_episode": 7.0,
                "sqz_range_high": 100.0, "sqz_range_low": 99.0,
                "sqz_atr": 0.25, "sqz_vwap24": 99.5, "sqz_compressed": 1.0,
            },
            {
                "timestamp": datetime(2026, 8, 20, 0, 45, tzinfo=UTC),
                "open": 99.0, "high": 100.5, "low": 98.0, "close": 100.2,
                "volume": 1200.0, "sqz_episode": 7.0,
                "sqz_range_high": 100.0, "sqz_range_low": 99.0,
                "sqz_atr": 0.25, "sqz_vwap24": 99.5, "sqz_compressed": 1.0,
            },
        ]
    )
    runner.on_closed_bar(bars, 0, bars.iloc[0]["timestamp"])
    entry_start = datetime(2026, 8, 20, 0, 43, tzinfo=UTC)
    for seconds in (0, 2, 5):
        runner.on_quote(bid=100.08, ask=100.09, ts=entry_start + timedelta(seconds=seconds))

    assert runner.has_open
    assert runner.open_meta is not None and runner.open_meta["entry_bar"] == 1
    runner.on_closed_bar(bars, 1, bars.iloc[1]["timestamp"])

    assert runner.has_open
    assert not [payload for kind, payload in journal.records if kind == "shadow_outcome"]
    assert runner.exits.pos is not None
    assert runner.exits.pos.mfe == 0.0
    assert runner.exits.pos.mae == 0.0


def test_shadow_runner_checks_protective_stop_on_each_quote() -> None:
    journal = _Journal()
    runner = SqueezeAcceptanceObserveRunner(journal=journal, symbol="BTC/USDT:USDT")
    bars = pd.DataFrame(
        [
            {
                "timestamp": datetime(2026, 8, 20, tzinfo=UTC),
                "open": 99.5,
                "high": 100.0,
                "low": 99.0,
                "close": 99.8,
                "volume": 1000.0,
                "sqz_episode": 7.0,
                "sqz_range_high": 100.0,
                "sqz_range_low": 99.0,
                "sqz_atr": 0.25,
                "sqz_vwap24": 99.5,
                "sqz_compressed": 1.0,
            }
        ]
    )
    runner.on_prepared_bar(bars, 0, bars.iloc[0]["timestamp"])
    t0 = datetime(2026, 8, 20, 0, 1, tzinfo=UTC)
    for seconds in (0, 2, 5):
        runner.on_quote(
            bid=100.08,
            ask=100.09,
            ts=t0 + timedelta(seconds=seconds),
        )
    assert runner.has_open

    # The very next BBO breaches the protective stop. No candle close is
    # required and no acceptance rule is consulted for the exit.
    runner.on_quote(
        bid=99.0,
        ask=99.01,
        ts=t0 + timedelta(seconds=6),
    )

    assert not runner.has_open
    outcomes = [payload for kind, payload in journal.records if kind == "shadow_outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["resolution"] == "stop_tick"
