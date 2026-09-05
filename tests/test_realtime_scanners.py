from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from vnedge.runtime.expansion_acceptance import (
    CompressionArm,
    ExpansionAcceptanceEngine,
)
from vnedge.strategy.realtime_scanners import (
    RANGE_ACCEPTANCE,
    HtfStructureContinuationRealtimeV1,
    RangeExpansionRealtimeV1,
    RangeExpansionRealtimeV2,
    SessionContinuationRealtimeV1,
    SessionContinuationRealtimeV2,
    StructureBosRealtimeV1,
    StructureBosRealtimeV2,
)
from vnedge.strategy.squeeze_expansion_breakout_v3 import SqueezeExpansionV3Params


def _canonical_h4_context(timestamp: str = "2026-08-24T08:00:00Z") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(timestamp),
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 10.0,
                "is_closed": True,
                "data_quality": "ok",
                "candle_source": "canonical_tick_lake",
            }
        ]
    )


def test_realtime_scanner_ids_can_never_emit_bar_close_entries() -> None:
    frame = pd.DataFrame({"close": [100.0]})
    for strategy in (
        RangeExpansionRealtimeV1(),
        RangeExpansionRealtimeV2(),
        StructureBosRealtimeV1(),
        StructureBosRealtimeV2(),
        SessionContinuationRealtimeV1(),
        SessionContinuationRealtimeV2(),
        HtfStructureContinuationRealtimeV1(),
    ):
        assert strategy.signal(frame, 0) is None


def test_range_successor_exposes_only_a_closed_bar_arm() -> None:
    strategy = RangeExpansionRealtimeV1()
    strategy.warmup_bars = 0
    frame = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-08-24T14:15:00Z"),
                "close": 100.0,
                "rt_arm_ready": 1.0,
                "rt_long_level": 101.0,
                "rt_short_level": 99.0,
                "rt_atr": 1.5,
                "rt_allow_long": 1.0,
                "rt_allow_short": 1.0,
            }
        ]
    )

    arm = strategy.realtime_arm(frame, 0)

    assert arm is not None
    assert arm.long_level == 101.0
    assert arm.short_level == 99.0
    assert arm.session_end_hour_utc == 16


def test_range_refreshes_share_one_episode_within_the_hour() -> None:
    strategy = RangeExpansionRealtimeV1()
    strategy.warmup_bars = 0
    rows = []
    for minute in (0, 15, 45):
        rows.append(
            {
                "timestamp": pd.Timestamp(f"2026-08-24T14:{minute:02d}:00Z"),
                "close": 100.0,
                "rt_arm_ready": 1.0,
                "rt_long_level": 101.0,
                "rt_short_level": 99.0,
                "rt_atr": 1.5,
                "rt_allow_long": 1.0,
                "rt_allow_short": 1.0,
            }
        )
    frame = pd.DataFrame(rows)

    episodes = [strategy.realtime_arm(frame, index).episode_id for index in range(3)]

    assert len(set(episodes)) == 1


def test_realtime_episode_is_invariant_to_frame_position() -> None:
    strategy = SessionContinuationRealtimeV1()
    strategy.warmup_bars = 0
    row = {
        "timestamp": pd.Timestamp("2026-08-24T14:15:00Z"),
        "close": 100.0,
        "rt_arm_ready": 1.0,
        "rt_long_level": 101.0,
        "rt_short_level": 99.0,
        "rt_atr": 1.5,
        "rt_allow_long": 1.0,
        "rt_allow_short": 1.0,
    }
    short_frame = pd.DataFrame([row])
    long_frame = pd.DataFrame(
        [
            {
                **row,
                "timestamp": pd.Timestamp("2026-08-24T14:00:00Z"),
                "rt_arm_ready": 0.0,
            },
            row,
        ]
    )

    short_arm = strategy.realtime_arm(short_frame, 0)
    long_arm = strategy.realtime_arm(long_frame, 1)

    assert short_arm is not None and long_arm is not None
    assert short_arm.episode_id == long_arm.episode_id


def test_range_v2_prearms_before_the_expanding_candle_exists() -> None:
    timestamps = pd.date_range(
        end="2026-08-24T11:45:00Z",
        periods=2_100,
        freq="15min",
    )
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": [100.05 if index % 2 else 99.95 for index in range(len(timestamps))],
            "volume": 100.0,
            "data_quality": "ok",
        }
    )
    old = RangeExpansionRealtimeV1()
    new = RangeExpansionRealtimeV2()

    old_frame = old.prepare(frame)
    new_frame = new.prepare(frame)
    index = len(frame) - 1

    assert old_frame.iloc[index]["rex3_expansion_ok"] == 0.0
    assert old.realtime_arm(old_frame, index) is None
    arm = new.realtime_arm(new_frame, index)
    assert arm is not None
    assert arm.allow_long is True
    assert arm.allow_short is True
    assert new_frame.iloc[index]["rt_decision_at"] == pd.Timestamp("2026-08-24T12:00:00Z")
    diagnostics = new.evaluation_diagnostics(new_frame, index)
    assert diagnostics["eligible"] is True
    assert diagnostics["features"]["setup_bar_requires_expansion"] is False


def test_session_v2_uses_close_time_for_the_session_boundary(monkeypatch) -> None:
    base = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-08-24T11:45:00Z"),
                "close": 100.0,
                "rs_quality_ok": 1.0,
                "rs_route_ok": 1.0,
                "sc15_session_ok": 0.0,
                "sc15_volume_ok": 1.0,
                "sc15_volume_ratio": 1.2,
                "rt_long_level": 101.0,
                "rt_short_level": 99.0,
                "rt_atr": 1.0,
                "rt_allow_long": 0.0,
                "rt_allow_short": 0.0,
                "rt_arm_ready": 0.0,
            },
            {
                "timestamp": pd.Timestamp("2026-08-24T15:45:00Z"),
                "close": 100.0,
                "rs_quality_ok": 1.0,
                "rs_route_ok": 1.0,
                "sc15_session_ok": 1.0,
                "sc15_volume_ok": 1.0,
                "sc15_volume_ratio": 1.2,
                "rt_long_level": 101.0,
                "rt_short_level": 99.0,
                "rt_atr": 1.0,
                "rt_allow_long": 1.0,
                "rt_allow_short": 1.0,
                "rt_arm_ready": 1.0,
            },
        ]
    )
    monkeypatch.setattr(
        SessionContinuationRealtimeV1,
        "prepare",
        lambda self, candles: base.copy(),
    )
    strategy = SessionContinuationRealtimeV2()
    strategy.warmup_bars = 0

    prepared = strategy.prepare(pd.DataFrame())
    arm = strategy.realtime_arm(prepared, 0)

    assert prepared.iloc[0]["rt_decision_at"] == pd.Timestamp("2026-08-24T12:00:00Z")
    assert prepared.iloc[0]["rt_session_ok"] == 1.0
    assert prepared.iloc[1]["rt_decision_at"] == pd.Timestamp("2026-08-24T16:00:00Z")
    assert prepared.iloc[1]["rt_session_ok"] == 0.0
    assert strategy.realtime_arm(prepared, 1) is None
    assert arm is not None
    diagnostics = strategy.evaluation_diagnostics(prepared, 0)
    assert diagnostics["eligible"] is True
    assert "continuation_level_not_broken" not in diagnostics["all_failed_gates"]


def test_bos_v2_uses_close_time_and_keeps_one_episode_for_the_same_swing(monkeypatch) -> None:
    rows = []
    for minute in (45,):
        rows.append(
            {
                "timestamp": pd.Timestamp(f"2026-08-24T11:{minute:02d}:00Z"),
                "open": 99.5,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 10.0,
                "is_closed": True,
                "data_quality": "ok",
                "candle_source": "canonical_tick_lake",
                "bos15_structure_ready": True,
                "bos15_quality_ok": 1.0,
                "bos15_session_ok": 0.0,
                "bos15_volume_ok": 1.0,
                "bos15_structure_trend": "up",
                "bos15_htf_structure_trend": "up",
                "bos15_dual_avwap_bias": "between",
                "bos15_projected_net_long_bps": 20.0,
                "bos15_projected_net_short_bps": 20.0,
                "rt_long_level": 101.0,
                "rt_short_level": 99.0,
                "rt_long_structural_stop": 98.0,
                "rt_short_structural_stop": 102.0,
                "rt_atr": 1.0,
                "rt_allow_long": 0.0,
                "rt_allow_short": 0.0,
                "rt_arm_ready": 0.0,
            }
        )
    base = pd.DataFrame(rows)
    monkeypatch.setattr(StructureBosRealtimeV1, "prepare", lambda self, candles: base.copy())
    strategy = StructureBosRealtimeV2()
    strategy.warmup_bars = 0
    strategy.bind_canonical_context("4h", _canonical_h4_context())

    prepared = strategy.prepare(pd.DataFrame())
    first = strategy.realtime_arm(prepared, 0)
    second = strategy.realtime_arm(prepared, 0)

    assert prepared.iloc[0]["rt_session_ok"] == 1.0
    assert first is not None and second is not None
    assert first.episode_id == second.episode_id
    diagnostics = strategy.evaluation_diagnostics(prepared, 0)
    assert diagnostics["eligible"] is True
    assert "confirmed_swing_not_broken" not in diagnostics["all_failed_gates"]


def test_quote_hold_supplies_entry_and_uses_structural_stop() -> None:
    config = SqueezeExpansionV3Params(
        acceptance_hold_seconds=0.5,
        min_acceptance_samples=2,
        break_buffer_bps=0.0,
        atr_stop_mult=1.5,
    )
    engine = ExpansionAcceptanceEngine(config=config)
    engine.update_arm(
        CompressionArm(
            episode_id=1,
            box_high=101.0,
            box_low=99.0,
            atr=1.0,
            vwap=100.0,
            bar_index=10,
            compressed=True,
            allow_long=True,
            allow_short=False,
            long_level=101.0,
            short_level=99.0,
            long_structural_stop=100.5,
            expires_after_bars=1,
            session_start_hour_utc=12,
            session_end_hour_utc=16,
            reason="structure_bos_realtime_v1",
        )
    )
    assert engine.last_reason == "armed_long"
    first = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)

    assert (
        engine.observe_quote(bid=101.1, ask=101.2, ts=first, received_ts=first, bar_index=10)
        is None
    )
    fire = engine.observe_quote(
        bid=101.1,
        ask=101.2,
        ts=first + timedelta(seconds=1),
        received_ts=first + timedelta(seconds=1),
        bar_index=10,
    )

    assert fire is not None
    assert fire.entry == 101.2
    assert fire.stop == 100.5
    assert fire.reason.startswith("structure_bos_realtime_v1")


def test_fill_time_session_gate_rejects_a_1600_quote() -> None:
    engine = ExpansionAcceptanceEngine(config=RANGE_ACCEPTANCE)
    engine.update_arm(
        CompressionArm(
            episode_id=2,
            box_high=101.0,
            box_low=99.0,
            atr=1.0,
            vwap=100.0,
            bar_index=12,
            compressed=True,
            long_level=101.0,
            short_level=99.0,
            session_start_hour_utc=12,
            session_end_hour_utc=16,
        )
    )
    ts = datetime(2026, 8, 24, 16, 0, tzinfo=UTC)

    assert engine.observe_quote(bid=101.1, ask=101.2, ts=ts, received_ts=ts, bar_index=12) is None
    assert engine.last_reason == "quote_outside_session"


def test_quote_engine_without_a_setup_reports_generic_arm_state() -> None:
    engine = ExpansionAcceptanceEngine(config=RANGE_ACCEPTANCE)
    ts = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)

    assert (
        engine.observe_quote(
            bid=100.0,
            ask=100.1,
            ts=ts,
            received_ts=ts,
            bar_index=1,
        )
        is None
    )
    assert engine.last_reason == "no_active_arm"


def test_htf_continuation_arm_uses_wide_structure_floor() -> None:
    strategy = HtfStructureContinuationRealtimeV1()
    strategy.warmup_bars = 0
    strategy.bind_canonical_context("4h", _canonical_h4_context())
    frame = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-08-24T14:15:00Z"),
                "open": 99.5,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 10.0,
                "is_closed": True,
                "data_quality": "ok",
                "candle_source": "canonical_tick_lake",
                "rt_arm_ready": 1.0,
                "rt_long_level": 101.0,
                "rt_short_level": 99.0,
                "rt_atr": 1.0,
                "rt_allow_long": 1.0,
                "rt_allow_short": 0.0,
                "rt_long_structural_stop": 99.5,
                "rt_short_structural_stop": 101.5,
            }
        ]
    )

    arm = strategy.realtime_arm(frame, 0)

    assert arm is not None
    assert arm.allow_long is True
    assert arm.allow_short is False
    assert arm.structural_stop_mode == "structure_floor"
    assert arm.expires_after_bars == 2
    assert arm.evidence is not None
    assert arm.evidence.context_bars[0].open_time == pd.Timestamp(
        "2026-08-24T08:00:00Z"
    )


def test_htf_continuation_rejects_arm_without_bound_4h_context() -> None:
    strategy = HtfStructureContinuationRealtimeV1()
    strategy.warmup_bars = 0
    frame = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-08-24T14:15:00Z"),
                "open": 99.5,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 10.0,
                "is_closed": True,
                "data_quality": "ok",
                "candle_source": "canonical_tick_lake",
                "rt_arm_ready": 1.0,
                "rt_long_level": 101.0,
                "rt_short_level": 99.0,
                "rt_atr": 1.0,
                "rt_allow_long": 1.0,
                "rt_allow_short": 0.0,
                "rt_long_structural_stop": 99.5,
                "rt_short_structural_stop": 101.5,
                "hsc_htf_aligned_long": 1.0,
                "hsc_htf_aligned_short": 0.0,
                "hsc_pullback_long": 1.0,
                "hsc_volume_ratio": 1.0,
                "hsc_body_bps": 20.0,
                "hsc_projected_net_long_bps": 20.0,
                "bos15_htf_structure_trend": "up",
                "bos15_structure_trend": "up",
            }
        ]
    )

    assert strategy.realtime_arm(frame, 0) is None
    diagnostics = strategy.evaluation_diagnostics(frame, 0)
    assert diagnostics["eligible"] is False
    assert diagnostics["primary_failed_gate"] == "htf_context_missing"


def test_htf_continuation_retains_bound_canonical_context() -> None:
    strategy = HtfStructureContinuationRealtimeV1()
    context = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-08-24T08:00:00Z"),
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 10.0,
            }
        ]
    )

    strategy.bind_canonical_context("4h", context)

    assert strategy.canonical_context_timeframes == ("4h",)
    assert strategy._structure._hourly.htf_candles is not None
    assert len(strategy._structure._hourly.htf_candles) == 1

    strategy.set_canonical_context_health("4h", False)
    assert strategy._structure._hourly._canonical_htf_current is False


def test_structure_floor_does_not_treat_small_pullback_as_reversal() -> None:
    config = SqueezeExpansionV3Params(
        acceptance_hold_seconds=0.5,
        min_acceptance_samples=2,
        break_buffer_bps=0.0,
        atr_stop_mult=1.25,
    )
    engine = ExpansionAcceptanceEngine(config=config)
    engine.update_arm(
        CompressionArm(
            episode_id=3,
            box_high=101.0,
            box_low=99.0,
            atr=1.0,
            vwap=100.0,
            bar_index=10,
            compressed=True,
            allow_long=True,
            allow_short=False,
            long_level=101.0,
            short_level=99.0,
            long_structural_stop=99.5,
            structural_stop_mode="structure_floor",
            reason="htf_structure_continuation_realtime_v1",
        )
    )
    first = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
    assert (
        engine.observe_quote(bid=101.1, ask=101.2, ts=first, received_ts=first, bar_index=10)
        is None
    )

    fire = engine.observe_quote(
        bid=101.1,
        ask=101.2,
        ts=first + timedelta(seconds=1),
        received_ts=first + timedelta(seconds=1),
        bar_index=10,
    )

    assert fire is not None
    assert fire.stop == 99.5
    assert fire.entry - fire.stop > config.atr_stop_mult


def test_htf_continuation_exits_only_after_confirmed_deterioration() -> None:
    strategy = HtfStructureContinuationRealtimeV1()
    intact = pd.DataFrame(
        [
            {
                "close": 101.0,
                "hsc_slow_ema": 100.0,
                "bos15_structure_trend": "up",
                "bos15_htf_structure_trend": "up",
            },
            {
                "close": 99.5,
                "hsc_slow_ema": 100.0,
                "bos15_structure_trend": "up",
                "bos15_htf_structure_trend": "up",
            },
        ]
    )
    assert strategy.exit_signal(intact, 1, "long", 101.0) is None

    deteriorated = pd.concat(
        [
            intact,
            pd.DataFrame(
                [
                    {
                        "close": 99.0,
                        "hsc_slow_ema": 100.0,
                        "bos15_structure_trend": "up",
                        "bos15_htf_structure_trend": "up",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    exit_intent = strategy.exit_signal(deteriorated, 2, "long", 101.0)
    assert exit_intent is not None
    assert exit_intent.reason == "htf_structure_deterioration"
