from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pandas as pd
import pytest

from vnedge.data.candles import Candle
from vnedge.data.regime_context import RegimeContext, RegimeLabel
from vnedge.risk.cost_gate import CostGate, CostProfile
from vnedge.strategy.structure_bos_1h import (
    PARAMS,
    STRATEGY_SPEC,
    Side,
    StructureBos1H,
    StructureBos1h,
    StructureContext,
    cost_profile,
    eligibility,
    evaluate_bos_intents,
    strategy_id,
    timeframe,
)

_PATTERN = [
    100,
    99,
    97,
    94,
    92,  # first confirmed low: low=91
    96,
    100,
    104,
    108,  # first confirmed high: high=109
    104,
    101,
    98,
    97,  # higher low: low=96
    101,
    106,
    111,
    113,  # higher high: high=114
    110,
    108,
    112,  # second high becomes visible here
    116,  # closed-bar bullish break above 114 + 5bps
]
_CLOSES = [100] * 35 + _PATTERN
_BREAK_INDEX = len(_CLOSES) - 1


def _canonical_frame(
    *,
    mirrored: bool = False,
    timeframe: str = "1h",
    start: str = "2026-01-01",
) -> pd.DataFrame:
    close = [float(value) for value in _CLOSES]
    high = [value + 1 for value in close]
    low = [value - 1 for value in close]
    if mirrored:
        close = [220 - value for value in close]
        high, low = [220 - value for value in low], [220 - value for value in high]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                start,
                periods=len(close),
                freq="4h" if timeframe == "4h" else "h",
                tz="UTC",
            ),
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": [10.0] * len(close),
            "quote_volume": [value * 10 for value in close],
            "trade_count": [10] * len(close),
            "data_quality": ["ok"] * len(close),
            "symbol": ["BTCUSDT"] * len(close),
            "timeframe": [timeframe] * len(close),
        }
    )


def _htf_frame(*, mirrored: bool = False) -> pd.DataFrame:
    frame = _canonical_frame(
        mirrored=mirrored,
        timeframe="4h",
        start="2025-12-20",
    )
    return frame


def _bars(frame: pd.DataFrame) -> list[Candle]:
    result: list[Candle] = []
    for row in frame.itertuples(index=False):
        opened = pd.Timestamp(row.timestamp).to_pydatetime()
        bar_timeframe = str(getattr(row, "timeframe", "1h"))
        hours = 4 if bar_timeframe == "4h" else 1
        result.append(
            Candle(
                symbol="BTCUSDT",
                timeframe=bar_timeframe,
                open_time=opened,
                close_time=opened + pd.Timedelta(hours=hours).to_pytimedelta(),
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=Decimal(str(row.volume)),
                quote_volume=Decimal(str(row.quote_volume)),
                trade_count=int(row.trade_count),
            )
        )
    return result


def _strategy(*, mirrored_htf: bool = False) -> StructureBos1H:
    return StructureBos1H(htf_candles=_htf_frame(mirrored=mirrored_htf))


def test_preregistration_is_frozen_and_non_capital() -> None:
    assert strategy_id == "structure_bos_1h"
    assert eligibility == "RESEARCH_ONLY"
    assert timeframe == "1h"
    assert cost_profile is CostProfile.SWING
    assert PARAMS.left == PARAMS.right == 3
    assert PARAMS.break_buffer_bps == 5
    assert PARAMS.stop_buffer_bps == 10
    assert PARAMS.atr_period == 14
    assert PARAMS.atr_stop_mult == Decimal("1.5")
    assert PARAMS.max_hold_hours == 48
    assert PARAMS.min_bars == 50
    assert PARAMS.cost_edge_reward_r == Decimal("1.5")
    assert PARAMS.min_room_cost_multiple == Decimal("1.5")
    assert PARAMS.regime_filter_enabled is True
    assert PARAMS.blocked_regime_labels == (
        "low_liquidity",
        "mean_reversion",
        "sideways",
        "unavailable",
    )
    assert STRATEGY_SPEC["capital_eligible"] is False
    assert STRATEGY_SPEC["tradeable"] is False
    assert StructureBos1h is StructureBos1H
    with pytest.raises(FrozenInstanceError):
        PARAMS.max_hold_hours = 24  # type: ignore[misc]
    with pytest.raises(ValueError, match="params are frozen"):
        StructureBos1H(params=replace(PARAMS, max_hold_hours=24))


def test_closed_candle_api_emits_deterministic_long_research_intent() -> None:
    engine = StructureBos1H()
    bars = _bars(_canonical_frame())
    bars_4h = _bars(_htf_frame())

    first = engine.on_closed_candle("BTCUSDT", bars, bars_4h, StructureContext())
    second = engine.on_closed_candle("BTCUSDT", bars, bars_4h, StructureContext())

    assert len(first) == 1
    assert first == second
    intent = first[0]
    assert intent.side is Side.LONG
    assert intent.signal_id == (f"structure_bos_1h:BTCUSDT:{intent.ts.strftime('%Y%m%d%H%M')}:long")
    assert intent.stop_ref < intent.entry_ref
    assert intent.time_stop - intent.ts == pd.Timedelta(hours=48).to_pytimedelta()
    assert intent.meta["break_buffer_bps"] == "5"
    assert intent.meta["mtf_alignment"] == "long"
    assert intent.meta["htf_trend"] == "up"


def test_closed_candle_api_fails_closed_on_quality_and_forming_bar() -> None:
    engine = StructureBos1H()
    bars = _bars(_canonical_frame())
    bars_4h = _bars(_htf_frame())

    assert (
        engine.on_closed_candle("BTCUSDT", bars, bars_4h, StructureContext(data_quality="gap"))
        == []
    )
    forming = [*bars[:-1], replace(bars[-1], is_closed=False)]
    assert engine.on_closed_candle("BTCUSDT", forming, bars_4h) == []


def test_closed_candle_api_applies_dual_avwap_conflict_only() -> None:
    engine = StructureBos1H()
    bars = _bars(_canonical_frame())
    bars_4h = _bars(_htf_frame())

    assert (
        engine.on_closed_candle(
            "BTCUSDT",
            bars,
            bars_4h,
            StructureContext(dual_avwap_bias="strong_short"),
        )
        == []
    )
    assert (
        len(
            engine.on_closed_candle(
                "BTCUSDT", bars, bars_4h, StructureContext(dual_avwap_bias="n/a")
            )
        )
        == 1
    )


def _regime(
    bars: list[Candle],
    label: RegimeLabel,
    *,
    as_of=None,
) -> RegimeContext:
    return RegimeContext(
        as_of=as_of or bars[-1].close_time,
        symbol="BTCUSDT",
        timeframe=bars[-1].timeframe,
        label=label,
        trend_direction=(
            "up"
            if label is RegimeLabel.TRENDING_UP
            else "down"
            if label is RegimeLabel.TRENDING_DOWN
            else "flat"
        ),
        adx=35.0,
        atr_percentile=0.5,
        ema_slope_bps=4.0,
        bb_width_bps=120.0,
        bb_width_percentile=0.5,
        volume_ratio=1.0,
        confidence=0.7,
        data_quality="ok",
        ready=True,
        reason="ok",
    )


def test_supplied_regime_context_blocks_opposition_and_adds_diagnostics() -> None:
    engine = StructureBos1H()
    bars_1h = _bars(_canonical_frame())
    bars_4h = _bars(_htf_frame())
    visible_4h = [bar for bar in bars_4h if bar.close_time <= bars_1h[-1].close_time]

    opposed = StructureContext(
        regime_1h=_regime(bars_1h, RegimeLabel.TRENDING_DOWN),
        regime_4h=_regime(visible_4h, RegimeLabel.TRENDING_UP),
    )
    assert engine.on_closed_candle("BTCUSDT", bars_1h, bars_4h, opposed) == []

    aligned = StructureContext(
        regime_1h=_regime(bars_1h, RegimeLabel.TRENDING_UP),
        regime_4h=_regime(visible_4h, RegimeLabel.TRENDING_UP),
    )
    intent = engine.on_closed_candle("BTCUSDT", bars_1h, bars_4h, aligned)[0]
    assert intent.meta["regime_1h"] == "trending_up"
    assert intent.meta["regime_4h"] == "trending_up"
    assert intent.meta["diagnostics_policy"] == "measurement_vector_no_grade"
    assert intent.meta["min_room_cost_multiple"] == "1.5"


def test_supplied_low_liquidity_regime_blocks_bos() -> None:
    engine = StructureBos1H()
    bars_1h = _bars(_canonical_frame())
    bars_4h = _bars(_htf_frame())
    context = StructureContext(
        regime_1h=_regime(bars_1h, RegimeLabel.LOW_LIQUIDITY),
    )

    assert engine.on_closed_candle("BTCUSDT", bars_1h, bars_4h, context) == []


def test_long_bos_is_confirmed_buffered_atr_capped_and_cost_approved() -> None:
    strategy = _strategy()
    prepared = strategy.prepare(_canonical_frame())
    second_high_confirmation = _BREAK_INDEX - 1

    assert prepared.loc[second_high_confirmation - 1, "last_swing_high"] == 109
    assert prepared.loc[second_high_confirmation, "last_swing_high"] == 114
    assert bool(prepared.loc[second_high_confirmation, "structure_ready"])
    assert prepared.loc[_BREAK_INDEX, "structure_trend"] == "up"
    assert prepared.loc[_BREAK_INDEX, "structure_labels"] == "HH,HL"
    assert prepared.loc[_BREAK_INDEX, "structure_event"] == "bos_up"
    assert prepared.loc[_BREAK_INDEX, "htf_structure_trend"] == "up"
    assert prepared.loc[_BREAK_INDEX, "mtf_alignment"] == "long"
    assert prepared.loc[_BREAK_INDEX, "mtf_reason"] == "htf_up_ltf_bos_up"

    evaluation = strategy.evaluate(prepared, _BREAK_INDEX)
    assert evaluation.accepted
    assert evaluation.candidate is not None
    assert evaluation.research_intent is not None
    assert evaluation.candidate.side == "long"
    swing_stop = 96 * 0.999
    atr_stop = 116 - 1.5 * float(prepared.loc[_BREAK_INDEX, "bos_atr"])
    assert evaluation.candidate.stop_price == pytest.approx(max(swing_stop, atr_stop))
    assert evaluation.candidate.take_profit_price == pytest.approx(
        116 + 1.5 * (116 - max(swing_stop, atr_stop))
    )
    assert evaluation.cost is not None and evaluation.cost.approved
    assert evaluation.cost.cost.total_cost_bps > 0
    assert evaluation.cost.available_room_bps is not None
    assert evaluation.cost.min_room_bps is not None
    assert evaluation.cost.available_room_bps >= evaluation.cost.min_room_bps

    intent = strategy.signal(prepared, _BREAK_INDEX)
    assert intent is not None
    assert "signal_id=structure_bos_1h:" in intent.reason
    assert "CostGate approved" in intent.reason


def test_short_bos_is_the_exact_mirror() -> None:
    strategy = _strategy(mirrored_htf=True)
    prepared = strategy.prepare(_canonical_frame(mirrored=True))

    evaluation = strategy.evaluate(prepared, _BREAK_INDEX)
    assert evaluation.accepted
    assert evaluation.candidate is not None
    assert evaluation.candidate.side == "short"
    swing_stop = 124 * 1.001
    atr_stop = 104 + 1.5 * float(prepared.loc[_BREAK_INDEX, "bos_atr"])
    assert evaluation.candidate.stop_price == pytest.approx(min(swing_stop, atr_stop))
    assert evaluation.cost is not None and evaluation.cost.approved


def test_missing_or_conflicting_htf_blocks_dataframe_signal() -> None:
    frame = _canonical_frame()
    missing = StructureBos1H().prepare(frame)
    conflict_strategy = _strategy(mirrored_htf=True)
    conflict = conflict_strategy.prepare(frame)

    assert missing.loc[_BREAK_INDEX, "mtf_alignment"] == "blocked"
    assert missing.loc[_BREAK_INDEX, "mtf_reason"] == "missing_series"
    assert StructureBos1H().signal(missing, _BREAK_INDEX) is None
    assert conflict.loc[_BREAK_INDEX, "mtf_alignment"] == "conflict"
    assert conflict.loc[_BREAK_INDEX, "mtf_reason"] == "htf_down_ltf_break_up"
    assert conflict_strategy.signal(conflict, _BREAK_INDEX) is None


def test_live_price_only_mode_derives_causal_4h_without_inventing_avwap() -> None:
    frame = _canonical_frame().drop(
        columns=["quote_volume", "trade_count", "data_quality"]
    )
    strategy = StructureBos1H(allow_price_only_live=True)
    prepared = strategy.prepare(frame)

    assert prepared.loc[_BREAK_INDEX, "mtf_reason"] != "missing_series"
    assert prepared.loc[_BREAK_INDEX, "mtf_reason"] != "invalid_series"
    assert prepared.loc[_BREAK_INDEX, "dual_avwap_bias"] == "unavailable"


def test_cost_gate_bridge_uses_frozen_edge_hypothesis() -> None:
    engine = StructureBos1H()
    raw = engine.on_closed_candle(
        "BTCUSDT",
        _bars(_canonical_frame()),
        _bars(_htf_frame()),
    )
    gate = CostGate(CostProfile.SWING, min_net_edge_bps=PARAMS.min_net_edge_bps)

    survived = evaluate_bos_intents(raw, gate, current_funding_rate=Decimal(0))

    assert len(survived) == 1
    assert survived[0][0] == raw[0]
    assert survived[0][1].approved


def test_cost_gate_rejection_is_reported_and_backtest_signal_is_dropped() -> None:
    strategy = _strategy()
    prepared = strategy.prepare(_canonical_frame())
    prepared.loc[_BREAK_INDEX, "funding_rate"] = 0.10

    evaluation = strategy.evaluate(prepared, _BREAK_INDEX)
    assert not evaluation.accepted
    assert evaluation.candidate is not None
    assert evaluation.research_intent is not None
    assert evaluation.cost is not None and not evaluation.cost.approved
    assert "net" in evaluation.reason
    assert strategy.signal(prepared, _BREAK_INDEX) is None


def test_data_quality_gap_cannot_create_or_bridge_structure() -> None:
    frame = _canonical_frame()
    frame.loc[_BREAK_INDEX - 5, "data_quality"] = "gap"
    strategy = _strategy()
    prepared = strategy.prepare(frame)

    assert not bool(prepared.loc[_BREAK_INDEX - 5, "structure_ready"])
    assert strategy.signal(prepared, _BREAK_INDEX) is None


def test_missing_canonical_integrity_fields_fail_closed() -> None:
    frame = _canonical_frame().drop(columns=["quote_volume", "data_quality"])
    strategy = _strategy()
    prepared = strategy.prepare(frame)

    assert not prepared["structure_ready"].any()
    assert all(strategy.signal(prepared, index) is None for index in range(len(prepared)))


def test_forming_bar_cannot_trigger_dataframe_adapter() -> None:
    frame = _canonical_frame()
    frame["is_closed"] = True
    frame.loc[_BREAK_INDEX, "is_closed"] = False
    strategy = _strategy()
    prepared = strategy.prepare(frame)

    assert not bool(prepared.loc[_BREAK_INDEX, "structure_ready"])
    assert strategy.signal(prepared, _BREAK_INDEX) is None


def test_prepare_and_signal_are_truncation_invariant_at_every_boundary() -> None:
    frame = _canonical_frame()
    htf = _htf_frame()
    full_strategy = StructureBos1H(htf_candles=htf)
    full = full_strategy.prepare(frame)
    columns = [
        "structure_ready",
        "structure_trend",
        "structure_labels",
        "structure_event",
        "mtf_alignment",
        "mtf_reason",
        "htf_structure_trend",
        "htf_structure_labels",
        "last_swing_high",
        "previous_swing_high",
        "last_swing_low",
        "previous_swing_low",
        "swing_low_avwap",
        "swing_high_avwap",
        "dual_avwap_bias",
        "bos_atr",
    ]
    for cut in range(7, len(frame) + 1):
        prefix_strategy = StructureBos1H(htf_candles=htf)
        prefix = prefix_strategy.prepare(frame.iloc[:cut].reset_index(drop=True))
        pd.testing.assert_frame_equal(
            full.iloc[:cut][columns].reset_index(drop=True),
            prefix[columns],
        )
        for index in range(cut):
            assert full_strategy.signal(full, index) == prefix_strategy.signal(prefix, index)
