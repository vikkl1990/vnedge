from __future__ import annotations

import pandas as pd

from vnedge.strategy.structure_bos_15m_trigger_v2 import (
    StructureBos15mTriggerV2,
    _complete_hour_frame,
)
from vnedge.strategy.structure_bos_15m_trigger_v3 import StructureBos15mTriggerV3


class _HourlyContext:
    def prepare(self, hours: pd.DataFrame) -> pd.DataFrame:
        out = hours.copy()
        out["structure_ready"] = True
        out["structure_trend"] = "up"
        out["last_swing_high"] = 101.0
        out["last_swing_low"] = 99.0
        out["dual_avwap_bias"] = "between"
        out["bos_atr"] = 1.0
        out["htf_structure_trend"] = "up"
        out["mtf_reason"] = "aligned"
        return out


def _history() -> pd.DataFrame:
    ts = pd.date_range(end="2026-08-22 12:15", periods=225, freq="15min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": ts,
            "symbol": "BTCUSDT",
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": 100.0,
            "data_quality": "ok",
            "is_closed": True,
        }
    )
    frame.loc[frame.index[-1], ["open", "high", "close", "volume"]] = [
        100.0,
        102.2,
        102.0,
        200.0,
    ]
    return frame


def test_v2_uses_closed_hour_context_and_15m_break_confirmation() -> None:
    strategy = StructureBos15mTriggerV2()
    strategy._hourly = _HourlyContext()
    prepared = strategy.prepare(_history())
    row = prepared.iloc[-1]

    assert row["bos15_structure_trend"] == "up"
    assert row["bos15_htf_structure_trend"] == "up"
    assert row["bos15_volume_ok"] == 1.0
    assert row["bos15_fire_long"] == 1.0
    signal = strategy.signal(prepared, len(prepared) - 1)
    assert signal is not None
    assert signal.side == "long"
    assert "context=closed_1h_4h" in signal.reason


def test_hour_frame_normalizes_equivalent_delta_symbol_spellings() -> None:
    frame = _history().iloc[:8].copy()
    frame.loc[frame.index[:4], "symbol"] = "BTCUSD"
    frame.loc[frame.index[4:], "symbol"] = "BTC/USD:USD"

    hourly = _complete_hour_frame(frame)

    assert not hourly.empty
    assert set(hourly["symbol"]) == {"BTCUSD"}


def test_hour_frame_rejects_actual_cross_market_contamination() -> None:
    frame = _history().iloc[:8].copy()
    frame.loc[frame.index[-1], "symbol"] = "ETH/USD:USD"

    try:
        _complete_hour_frame(frame)
    except ValueError as exc:
        assert "multiple symbols" in str(exc)
    else:  # pragma: no cover - defensive assertion for a safety boundary
        raise AssertionError("cross-market candle series was accepted")


def test_v2_rejects_conflicting_higher_timeframe() -> None:
    strategy = StructureBos15mTriggerV2()
    context = _HourlyContext()
    original = context.prepare

    def prepare(hours: pd.DataFrame) -> pd.DataFrame:
        out = original(hours)
        out["htf_structure_trend"] = "down"
        return out

    context.prepare = prepare  # type: ignore[method-assign]
    strategy._hourly = context

    row = strategy.prepare(_history()).iloc[-1]

    assert row["bos15_fire_long"] == 0.0


def test_v2_diagnostics_name_higher_timeframe_conflict() -> None:
    strategy = StructureBos15mTriggerV2()
    context = _HourlyContext()
    original = context.prepare

    def prepare(hours: pd.DataFrame) -> pd.DataFrame:
        out = original(hours)
        out["htf_structure_trend"] = "down"
        return out

    context.prepare = prepare  # type: ignore[method-assign]
    strategy._hourly = context
    prepared = strategy.prepare(_history())

    report = strategy.evaluation_diagnostics(prepared, len(prepared) - 1)

    assert report["eligible"] is False
    assert report["primary_failed_gate"] == "htf_structure_conflict"
    assert report["features"]["bos15_structure_trend"] == "up"
    assert report["features"]["bos15_htf_structure_trend"] == "down"


def test_v3_preserves_v2_setup_and_emits_corrected_identity() -> None:
    strategy = StructureBos15mTriggerV3()
    strategy._hourly = _HourlyContext()
    prepared = strategy.prepare(_history())

    assert prepared.iloc[-1]["bos15_v3_final_eligible_long"] == 1.0
    signal = strategy.signal(prepared, len(prepared) - 1)
    assert signal is not None
    assert "structure_bos_15m_trigger_v3" in signal.reason
