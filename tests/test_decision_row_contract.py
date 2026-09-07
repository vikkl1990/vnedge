from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.strategy.arm_evidence import assert_decision_row, bar_content_sha256
from vnedge.strategy.base_strategy import SignalIntent, bind_signal_decision


def _row(**changes):
    row = {
        "timestamp": datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 10.0,
        "quote_volume": 1005.0,
        "trade_count": 4,
        "is_closed": True,
        "data_quality": "ok",
        "candle_source": "canonical_tick_lake",
    }
    row.update(changes)
    opened = row["timestamp"]
    row["content_sha256"] = bar_content_sha256(
        row,
        open_time=opened,
        close_time=opened + timedelta(minutes=15),
        source=str(row["candle_source"]),
    )
    return row


def test_decision_row_accepts_only_hashed_canonical_closed_truth() -> None:
    ref = assert_decision_row(_row(), timeframe="15m")

    assert ref.source == "canonical_tick_lake"
    assert ref.content_sha256 is not None and len(ref.content_sha256) == 64


def test_decision_row_rejects_missing_or_drifted_content_hash() -> None:
    missing = _row()
    missing.pop("content_sha256")
    with pytest.raises(ValueError, match="decision_row_content_hash_missing"):
        assert_decision_row(missing, timeframe="15m")

    drifted = _row()
    drifted["content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="decision_row_content_hash_mismatch"):
        assert_decision_row(drifted, timeframe="15m")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"is_closed": False}, "decision_row_not_closed"),
        ({"data_quality": "gap"}, "decision_row_quality_not_ok"),
        ({"candle_source": "exchange_ohlcv"}, "untrusted permission candle source"),
        ({"timestamp": datetime(2026, 9, 6, 12, 1, tzinfo=UTC)}, "not_timeframe_aligned"),
        ({"close": float("nan")}, "decision_row_close_invalid"),
    ],
)
def test_decision_row_rejects_noncanonical_inputs(changes, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        assert_decision_row(_row(**changes), timeframe="15m")


def test_registered_htf_scanner_cannot_arm_without_snapshot() -> None:
    signal = SignalIntent(side="long", stop_price=99.0)

    with pytest.raises(ValueError, match="required permission snapshot missing"):
        bind_signal_decision(
            signal,
            strategy_id="structure_bos_15m_trigger_v2",
            symbol="BTC/USD:USD",
            timeframe="15m",
            decision_row=_row(),
            entry_clock="next_15m_open",
        )


def test_context_free_scanner_gets_decision_bar_only_snapshot() -> None:
    signal = bind_signal_decision(
        SignalIntent(side="long", stop_price=99.0),
        strategy_id="avwap_reclaim_15m_v1",
        symbol="BTC/USD:USD",
        timeframe="15m",
        decision_row=_row(),
        entry_clock="next_15m_open",
    )

    assert signal.decision_envelope is not None
    assert signal.permission_snapshot is not None
    assert signal.permission_snapshot.context_bars == ()


def test_strict_binding_requires_upstream_content_hash() -> None:
    row = _row()
    row.pop("content_sha256")

    with pytest.raises(ValueError, match="decision_row_content_hash_missing"):
        bind_signal_decision(
            SignalIntent(side="long", stop_price=99.0),
            strategy_id="avwap_reclaim_15m_v1",
            symbol="BTC/USD:USD",
            timeframe="15m",
            decision_row=row,
            entry_clock="next_15m_open",
            require_canonical_truth=True,
        )


def test_strict_context_free_binding_keeps_the_stamped_bar_identity() -> None:
    row = _row()

    signal = bind_signal_decision(
        SignalIntent(side="long", stop_price=99.0),
        strategy_id="avwap_reclaim_15m_v1",
        symbol="BTC/USD:USD",
        timeframe="15m",
        decision_row=row,
        entry_clock="next_15m_open",
        require_canonical_truth=True,
    )

    assert signal.permission_snapshot is not None
    assert signal.permission_snapshot.decision_bar.content_sha256 == row["content_sha256"]
