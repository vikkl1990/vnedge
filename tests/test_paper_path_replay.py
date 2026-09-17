"""Admission tests use synthetic rows; they establish no historical coverage."""
import pandas as pd
import pytest

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.research.paper_path_replay import load_verified_frame, replay_bundle


def rows(timeframe="15min", periods=3):
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2026-09-01", periods=periods, freq=timeframe.replace("d", "D"), tz="UTC"),
        "open": 100., "high": 102., "low": 99., "close": 101., "volume": 10.,
        "exchange": "delta_india", "symbol": "BTC/USD:USD", "is_closed": True,
        "coverage_ok": True, "data_quality": "ok", "candle_source": "canonical_tick_lake",
    })
    frame["content_sha256"] = [bar_content_sha256(
        r.to_dict(), open_time=r.timestamp.to_pydatetime(),
        close_time=(r.timestamp + pd.Timedelta(timeframe.replace("d", "D"))).to_pydatetime(),
        source="canonical_tick_lake") for _, r in frame.iterrows()]
    return frame


def test_accepts_valid_fixture_without_fabricating_fields(tmp_path):
    frame = rows()
    path = tmp_path / "bars.parquet"
    frame.to_parquet(path)
    pd.testing.assert_frame_equal(load_verified_frame(path, "15m", "BTC/USD:USD"), frame)


@pytest.mark.parametrize("defect", ["duplicate", "gap", "hash", "source", "symbol", "coverage", "closed"])
def test_refuses_bad_input_before_replay(tmp_path, defect):
    frame = rows()
    if defect == "duplicate":
        frame = pd.concat([frame, frame.tail(1)])
    elif defect == "gap":
        frame = frame.drop(index=1)
    else:
        column, value = {"hash": ("close", 100.5), "source": ("candle_source", "exchange_ohlcv"),
                         "symbol": ("symbol", "ETH/USD:USD"), "coverage": ("coverage_ok", False),
                         "closed": ("is_closed", False)}[defect]
        frame.loc[0, column] = value
    path = tmp_path / "bars.parquet"
    frame.to_parquet(path)
    with pytest.raises(ValueError):
        load_verified_frame(path, "15m", "BTC/USD:USD")


async def test_killed_or_unregistered_strategy_refused_before_input_read(tmp_path):
    for strategy in ("funding_mean_reversion_v1", "made_up"):
        with pytest.raises(ValueError, match="frozen"):
            await replay_bundle(strategy_id=strategy, candles=tmp_path / "missing",
                                h4=tmp_path / "missing", daily=tmp_path / "missing",
                                output=tmp_path / "output")
    assert not (tmp_path / "output").exists()


async def test_real_registered_strategy_bundle_reports_no_setup_not_success(tmp_path):
    """Real scanner, artificial input: tests plumbing, not historical opportunity."""
    from vnedge.strategy.htf_regime_continuation_15m_v2_pairs import BTC_STRATEGY_ID
    paths = {}
    for tf, count in (("15min", 227), ("4h", 24), ("1d", 4)):
        path = tmp_path / f"{tf}.parquet"
        rows(tf, count).to_parquet(path)
        paths[tf] = path
    result = await replay_bundle(strategy_id=BTC_STRATEGY_ID, candles=paths["15min"],
                                 h4=paths["4h"], daily=paths["1d"], output=tmp_path / "run")
    assert result["status"] == "INCOMPLETE"
    assert result["run"]["fills"] == 0
    assert result["edge_support"]["supported"] is False
    assert result["events"]["lane_eval"] > 0
    assert (tmp_path / "run" / "manifest.json").exists()
    with pytest.raises(FileExistsError):
        await replay_bundle(strategy_id=BTC_STRATEGY_ID, candles=paths["15min"],
                            h4=paths["4h"], daily=paths["1d"], output=tmp_path / "run")
