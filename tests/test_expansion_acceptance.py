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

    runner.on_prepared_bar(bars, 1, bars.iloc[1]["timestamp"])
    outcomes = [payload for kind, payload in journal.records if kind == "shadow_outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["net_won"] is False
    assert outcomes[0]["net_bps"] < outcomes[0]["captured_bps"]
    assert runner.acceptance.long.state is AcceptanceState.ARMED
