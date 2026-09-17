"""Admission tests use synthetic rows; they establish no historical coverage."""
import json
import pandas as pd
import pytest

from vnedge.data.bar_identity import bar_content_sha256
from vnedge.research.paper_path_replay import load_verified_frame, replay_bundle, preflight_bundle
from vnedge.strategy.htf_regime_continuation_15m_v2_pairs import BTC_STRATEGY_ID


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


def official_rows(tf="4h", count=3):
    frame = rows(tf, count)
    frame["candle_source"] = "exchange_ohlcv_validated"
    # Real overlays introduce null optional trade measurements on official
    # rows. These are absent measurements, not fabricated tape or zero volume.
    frame["quote_volume"] = float("nan")
    frame["trade_count"] = float("nan")
    frame["content_sha256"] = [bar_content_sha256(
        r.to_dict(), open_time=r.timestamp.to_pydatetime(),
        close_time=(r.timestamp + pd.Timedelta(tf.replace("d", "D"))).to_pydatetime(),
        source="exchange_ohlcv_validated") for _, r in frame.iterrows()]
    return frame


def test_registered_context_is_accepted_without_changing_its_bytes(tmp_path):
    frame = official_rows()
    path = tmp_path / "h4.parquet"
    frame.to_parquet(path)
    before = path.read_bytes()
    loaded = load_verified_frame(path, "4h", "BTC/USD:USD", strategy_id=BTC_STRATEGY_ID,
                                 source_policy="registered_context_v1")
    pd.testing.assert_frame_equal(frame, loaded)
    assert path.read_bytes() == before
    assert loaded.quote_volume.isna().all()
    with pytest.raises(ValueError, match="untrusted"):
        load_verified_frame(path, "4h", "BTC/USD:USD", strategy_id=BTC_STRATEGY_ID)


def test_official_decision_stays_refused_under_registered_profile(tmp_path):
    path = tmp_path / "decision.parquet"
    official_rows("15min").to_parquet(path)
    with pytest.raises(ValueError, match="untrusted"):
        load_verified_frame(path, "15m", "BTC/USD:USD", strategy_id=BTC_STRATEGY_ID,
                            source_policy="registered_context_v1")


@pytest.mark.parametrize("defect", ["hash", "source", "tf", "open_time", "ohlc", "closed_text"])
def test_context_profile_does_not_accept_forged_or_invalid_rows(tmp_path, defect):
    frame = official_rows()
    if defect == "hash":
        frame.loc[0, "close"] = 101.5
    elif defect == "source":
        frame["source"] = "canonical_tick_lake"
    elif defect == "tf":
        frame["timeframe"] = "1d"
    elif defect == "open_time":
        frame["open_time"] = frame.timestamp + pd.Timedelta(hours=4)
    elif defect == "ohlc":
        frame.loc[0, "high"] = 90.
    else:
        frame["is_closed"] = "False"
    path = tmp_path / "context.parquet"
    frame.to_parquet(path)
    with pytest.raises(ValueError):
        load_verified_frame(path, "4h", "BTC/USD:USD", strategy_id=BTC_STRATEGY_ID,
                            source_policy="registered_context_v1")


def test_preflight_reports_all_missing_inputs_and_writes_nothing(tmp_path):
    result = preflight_bundle(strategy_id=BTC_STRATEGY_ID, candles=tmp_path / "a",
                             h4=tmp_path / "b", daily=tmp_path / "c",
                             source_policy="registered_context_v1")
    assert result["status"] == "INPUTS_REJECTED"
    assert {i["timeframe"] for i in result["issues"]} == {"15m", "4h", "1d"}
    assert not list(tmp_path.iterdir())


def test_preflight_rejects_unlabelled_cache_not_promoting_ohlc(tmp_path):
    path = tmp_path / "cache.parquet"
    rows().drop(columns=["exchange", "symbol", "coverage_ok", "content_sha256"]).to_parquet(path)
    result = preflight_bundle(strategy_id=BTC_STRATEGY_ID, candles=path, h4=path, daily=path,
                             source_policy="registered_context_v1")
    assert result["status"] == "INPUTS_REJECTED"
    assert len(result["issues"]) == 3


async def test_registered_replay_records_source_policy_and_causal_context_refs(tmp_path):
    decision, h4, daily = (tmp_path / name for name in ("decision", "h4", "daily"))
    rows(periods=227).to_parquet(decision)
    official_rows("4h", 24).to_parquet(h4)
    official_rows("1d", 4).to_parquet(daily)
    result = preflight_bundle(strategy_id=BTC_STRATEGY_ID, candles=decision, h4=h4, daily=daily,
                             source_policy="registered_context_v1")
    assert result["status"] == "INPUTS_VERIFIED"
    assert result["asof_boundary_examples"]["first"] == {"4h": None, "1d": None}
    last = result["asof_boundary_examples"]["last"]
    cutoff = rows(periods=227).timestamp.iloc[-1] + pd.Timedelta(minutes=15)
    assert all(pd.Timestamp(ref["close_time"]) <= cutoff for ref in last.values())
    assert last["1d"]["open_time"] == "2026-09-02T00:00:00+00:00"
    replay = await replay_bundle(strategy_id=BTC_STRATEGY_ID, candles=decision, h4=h4, daily=daily,
                                 output=tmp_path / "run", source_policy="registered_context_v1")
    assert replay["source_policy"] == result["source_policy"]
    assert replay["status"] == "INCOMPLETE"  # no artificial signal / edge estimate
    assert replay["edge_support"]["supported"] is False
    assert replay["can_trade"] is replay["performance_eligible"] is False
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text())
    assert manifest["config"]["max_holding_bars"] == 192


def test_preflight_cli_failure_is_nonzero_and_writes_nothing(tmp_path, monkeypatch, capsys):
    from vnedge.research.paper_path_replay import main

    monkeypatch.setattr("sys.argv", ["replay", "--strategy", BTC_STRATEGY_ID,
        "--candles", str(tmp_path / "a"), "--h4", str(tmp_path / "b"),
        "--daily", str(tmp_path / "c"), "--check-inputs-only",
        "--source-policy", "registered_context_v1"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "INPUTS_REJECTED"
    assert not list(tmp_path.iterdir())
