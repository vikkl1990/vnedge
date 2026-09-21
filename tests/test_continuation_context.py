from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pandas as pd
import pytest

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.data.candles import Candle
from vnedge.strategy.continuation_context import ContinuationContext
from vnedge.strategy.htf_regime_continuation_15m import HtfRegimeContinuation15mV1
from vnedge.strategy.htf_regime_continuation_15m_v2 import HtfRegimeContinuation15mV2
from vnedge.strategy.market_regime import MarketRegime, MarketRegimeMachine
from vnedge.strategy.realtime_scanners import HtfStructureContinuationRealtimeV1


def test_context_is_isolated_normalized_and_asof_closed() -> None:
    first = ContinuationContext(MarketRegimeMachine)
    other = ContinuationContext(MarketRegimeMachine)
    frame = pd.DataFrame({
        "open_time": pd.to_datetime([
            "2026-09-01T04:00Z", "2026-09-01T00:00Z", "2026-09-01T00:00Z",
        ]),
        "close": [103., 100., 101.],
    })
    first.bind_canonical_context("4h", frame)
    assert first.frames["4h"]["close"].tolist() == [101., 103.]
    frame.loc[2, "close"] = 999.
    assert first.frames["4h"].iloc[0]["close"] == 101.
    assert other.frames["4h"].empty
    assert other.live_machine is not first.live_machine
    assert first.identity(pd.Timestamp("2026-09-01T03:59Z"))[1] is None
    assert first.identity(pd.Timestamp("2026-09-01T04:00Z"))[1] == pd.Timestamp(
        "2026-09-01T00:00Z"
    ).value
    assert first.identity(pd.Timestamp("2026-09-01T07:59Z"))[1] == pd.Timestamp(
        "2026-09-01T00:00Z"
    ).value


def test_health_loss_and_recovery_do_not_discard_evidence() -> None:
    context = ContinuationContext(MarketRegimeMachine)
    frame = pd.DataFrame({"timestamp": [pd.Timestamp("2026-09-01T00:00Z")]})
    for tf in ("4h", "1d"):
        context.bind_canonical_context(tf, frame)
    at = pd.Timestamp("2026-09-02T00:00Z")
    machine = Mock(spec=MarketRegimeMachine)
    for healthy in (True, False, True):
        context.set_canonical_context_health("4h", healthy)
        context.regime_at(at, machine=machine)
        assert machine.step.call_args.kwargs["data_healthy"] is healthy
        assert context.identity(at)[0] is healthy
        pd.testing.assert_frame_equal(context.frames["4h"], frame)
    with pytest.raises(ValueError, match="unsupported"):
        context.set_canonical_context_health("15m", True)


def test_ingest_preserves_hash_deduplication_and_retention() -> None:
    context = ContinuationContext(MarketRegimeMachine)
    opened = datetime(2026, 9, 1, tzinfo=UTC)
    context.bind_canonical_context("4h", pd.DataFrame({
        "timestamp": pd.date_range(end=opened - timedelta(hours=4), periods=800, freq="4h"),
        "close": 100.,
    }))
    candle = Candle(
        symbol="BTCUSD", timeframe="4h", open_time=opened,
        close_time=opened + timedelta(hours=4), open=Decimal(100),
        high=Decimal(103), low=Decimal(99), close=Decimal(102),
        volume=Decimal(10), quote_volume=Decimal(1020), trade_count=20,
    )
    context.ingest_canonical_context(candle)
    context.ingest_canonical_context(candle)
    assert len(context.frames["4h"]) == 800
    row = context.frames["4h"].iloc[-1]
    assert row["content_sha256"] == bar_content_sha256(
        row.to_dict(), open_time=candle.open_time, close_time=candle.close_time,
        source="canonical_tick_lake",
    )
    assert context.frames["4h"]["timestamp"].is_unique
    assert context.health["4h"]


def test_weekly_artifacts_stay_separate_and_invalid_bind_preserves_cache() -> None:
    context = ContinuationContext(MarketRegimeMachine)
    opens = pd.date_range("2024-01-01", periods=70, freq="7D", tz="UTC")
    frame = pd.DataFrame({
        "open_time": opens, "close_time": opens + pd.Timedelta(days=7),
        "exchange": "delta_india", "symbol": "BTCUSD", "timeframe": "1w",
        "vwap": 100., "sum_base": 1., "sum_notional": 100., "n_trades": 1,
        "source": "trade_lake", "coverage_ok": True,
    })
    context.bind_weekly_vwap_artifacts(frame)
    assert len(context.weekly_vwap_artifacts) == 64
    assert context.weekly_vwap_symbol == "BTCUSD"
    assert context.frames["1d"].empty
    saved = context.weekly_vwap_artifacts.copy()
    with pytest.raises(ValueError, match="missing columns"):
        context.bind_weekly_vwap_artifacts(frame.drop(columns="source"))
    pd.testing.assert_frame_equal(context.weekly_vwap_artifacts, saved)
    context.bind_weekly_vwap_artifacts(pd.DataFrame())
    assert context.weekly_vwap_artifacts.empty
    assert context.weekly_vwap_symbol is None


@pytest.mark.parametrize("scanner", [HtfRegimeContinuation15mV1, HtfRegimeContinuation15mV2])
def test_full_fold_hands_same_machine_to_latest_and_replay_replaces_it(monkeypatch, scanner):
    machines = []

    def factory(self):
        machine = Mock(spec=MarketRegimeMachine)
        machine.step.return_value = MarketRegime(
            weekly="up", daily="mid", h4="up", allow_long=True, allow_short=False,
            state="continuation", reason="fixture", ema_state="up", macd_impulse="on",
        )
        machines.append(machine)
        return machine

    monkeypatch.setattr(scanner, "_new_regime_machine", factory)
    monkeypatch.setattr(HtfStructureContinuationRealtimeV1, "prepare", lambda self, f: f.copy())
    strategy = scanner()
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2026-09-01", periods=3, freq="15min", tz="UTC"),
        "rt_allow_long": 1., "rt_allow_short": 0.,
        "bos15_structure_ready": True, "bos15_quality_ok": 1.,
        "bos15_structure_trend": "up", "hsc_volume_ratio": 2., "hsc_body_bps": 100.,
        "hsc_pullback_long": 1., "hsc_pullback_short": 0.,
        "hsc_projected_net_long_bps": 100., "hsc_projected_net_short_bps": 100.,
    })
    strategy.prepare(frame.iloc[:2])
    replay_machine = machines[-1]
    assert replay_machine is strategy._context.live_machine
    assert replay_machine.step.call_count == 1  # Same closed-context identity.
    latest = strategy.prepare_latest(frame)
    assert strategy._context.live_machine is replay_machine
    assert replay_machine.step.call_count == 2
    assert replay_machine.step.call_args.kwargs["as_of"] == pd.Timestamp("2026-09-01T00:45Z")
    assert latest.iloc[:-1][["mreg_ready", "mreg_allow_long", "rt_arm_ready"]].eq(0).all().all()
    assert latest.attrs["vnedge_latest_only"]
    strategy.prepare(frame)
    assert strategy._context.live_machine is not replay_machine
    assert replay_machine.step.call_count == 2
