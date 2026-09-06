from __future__ import annotations

import pandas as pd

from vnedge.strategy.structure_bos_15m_trigger_v2 import (
    StructureBos15mTriggerV2,
    _complete_hour_frame,
)
from vnedge.strategy.structure_bos_15m_trigger_v3 import StructureBos15mTriggerV3


class _HourlyContext:
    _canonical_htf_current = True
    htf_candles = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-08-22 08:00", tz="UTC")],
            "open": [99.0],
            "high": [103.0],
            "low": [98.0],
            "close": [102.0],
            "volume": [1_000.0],
            "data_quality": ["ok"],
            "is_closed": [True],
            "candle_source": ["canonical_tick_lake"],
        }
    )

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
            "candle_source": "canonical_tick_lake",
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
    assert signal.permission_snapshot is not None
    assert signal.permission_snapshot.context_bars[0].open_time == pd.Timestamp(
        "2026-08-22 08:00", tz="UTC"
    )


def test_v2_rejects_fire_when_bound_hour_context_disappears() -> None:
    strategy = StructureBos15mTriggerV2()
    context = _HourlyContext()
    strategy._hourly = context
    prepared = strategy.prepare(_history())
    context.htf_candles = pd.DataFrame()

    assert strategy.signal(prepared, len(prepared) - 1) is None
    diagnostics = strategy.evaluation_diagnostics(prepared, len(prepared) - 1)
    assert diagnostics["primary_failed_gate"] == "htf_context_missing"
    assert diagnostics["eligible"] is False


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


def test_v2_does_not_carry_stale_hour_parent_across_missing_hour() -> None:
    strategy = StructureBos15mTriggerV2()
    strategy._hourly = _HourlyContext()
    frame = _history()
    missing_hour = frame["timestamp"].dt.floor("h").iloc[-1] - pd.Timedelta(hours=1)
    frame = frame.loc[frame["timestamp"].dt.floor("h") != missing_hour].reset_index(drop=True)

    prepared = strategy.prepare(frame)
    expected_hour = prepared["timestamp"].dt.floor("h")
    stale = prepared["bos15_parent_available_at"].notna() & (
        prepared["bos15_parent_available_at"] != expected_hour
    )

    assert stale.any()  # preserved for evidence, never treated as current
    missing = prepared.loc[~prepared["bos15_parent_identity_ok"]]
    assert not missing.empty
    assert missing["bos15_structure_trend"].eq("unavailable").all()


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
