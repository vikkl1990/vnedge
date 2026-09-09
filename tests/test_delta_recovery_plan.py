from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib

import pandas as pd
import pytest

from vnedge.data.candles import Candle, CandleParquetStore, aggregate_candle_series
from vnedge.data.delta_recovery_plan import build_delta_recovery_plan, build_delta_recovery_report

START = datetime(2026, 8, 31, tzinfo=UTC)


def seed(tmp_path):
    store = CandleParquetStore(tmp_path / "candles", exchange="delta_india")
    candles = [Candle(symbol="BTCUSD", timeframe="1m", open_time=START + timedelta(minutes=i),
        close_time=START + timedelta(minutes=i+1), open=Decimal(100), high=Decimal(100),
        low=Decimal(100), close=Decimal(100), volume=Decimal("0.01"), quote_volume=Decimal(1),
        trade_count=1, taker_buy_volume=Decimal("0.01"), vwap=Decimal(100)) for i in range(60)]
    store.upsert(candles)
    return store, candles


def plan(tmp_path, hours=1):
    return build_delta_recovery_plan(tmp_path, tmp_path / "candles", symbol="BTCUSD",
                                    required_hours=hours, as_of=START + timedelta(hours=1))


def inventory(tmp_path):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}


def test_complete_children_plan_is_read_only_and_chained(tmp_path):
    seed(tmp_path)
    before = inventory(tmp_path)
    result = plan(tmp_path)
    assert inventory(tmp_path) == before
    assert result["counts"] == {"REBUILD_FROM_VERIFIED_CHILDREN": 1}
    assert result["rebuild_candidate_count"] == 17  # 12 five-minute, 4 fifteen-minute, 1 hourly
    hourly = result["rebuild_candidates"][-1]
    assert hourly["timeframe"] == "1h" and hourly["requires_parent_chain"]
    assert len(hourly["child_hashes"]) == 4
    assert not result["can_apply"] and not result["can_trade"] and not result["can_promote"]
    assert plan(tmp_path)["plan_id"] == result["plan_id"]


@pytest.mark.parametrize("bad", ["gap", "closed", "source", "hash", "symbol", "duplicate", "coverage"])
def test_bad_child_does_not_prove_parent(tmp_path, bad):
    store, candles = seed(tmp_path)
    path = store.partition_path(candles[0])
    frame = pd.read_parquet(path)
    if bad == "gap": frame = frame.drop(index=5)
    if bad == "closed": frame.loc[5, "is_closed"] = False
    if bad == "source": frame.loc[5, "source"] = "official_delta_ohlc"
    if bad == "hash": frame.loc[5, "content_sha256"] = "a" * 64
    if bad == "symbol": frame["symbol"] = "ETHUSD"
    if bad == "duplicate": frame = pd.concat([frame, frame.iloc[[5]]])
    if bad == "coverage": frame["coverage_ok"] = "true"
    frame.to_parquet(path)
    result = plan(tmp_path)
    assert "REBUILD_FROM_VERIFIED_CHILDREN" not in result["counts"]
    assert not any(c["timeframe"] == "1h" for c in result["rebuild_candidates"])


def test_raw_day_is_not_interval_coverage(tmp_path):
    tape = tmp_path / "ticks/exchange=delta_india/symbol=BTCUSD/stream=trades/20260831/part.parquet"
    tape.parent.mkdir(parents=True)
    pd.DataFrame({"price": [100]}).to_parquet(tape)
    result = plan(tmp_path, hours=2)
    assert result["counts"] == {"NO_LOCAL_RAW_DAY": 1, "RAW_DAY_PRESENT_COVERAGE_UNPROVEN": 1}
    assert result["raw_completeness"] == "UNPROVEN_NO_SUPPORTED_MANIFEST"
    assert not result["rebuild_candidates"]


def test_invalid_existing_parent_not_replaced(tmp_path):
    store, candles = seed(tmp_path)
    parent = aggregate_candle_series("BTCUSD", "1m", "1h", candles)
    store.upsert(parent, coverage_ok=False)
    result = plan(tmp_path)
    assert result["counts"] == {"PRESENT_PROOF_INVALID": 1}
    assert not any(c["timeframe"] == "1h" for c in result["rebuild_candidates"])


def test_leading_history_shortfall_and_current_hour_excluded(tmp_path):
    store, candles = seed(tmp_path)
    store.upsert(aggregate_candle_series("BTCUSD", "1m", "1h", candles))
    result = plan(tmp_path, hours=2160)
    assert result["counts"] == {"NO_LOCAL_RAW_DAY": 2159, "VERIFIED": 1}
    assert result["ranges"][0]["close_time"] == START.isoformat()
    assert result["window_close"] == (START + timedelta(hours=1)).isoformat()


def test_unreadable_is_not_missing_and_fingerprint_changes(tmp_path):
    store, candles = seed(tmp_path)
    old = plan(tmp_path)
    path = store.partition_path(candles[0])
    frame = pd.read_parquet(path).iloc[:-1]
    frame.to_parquet(path)
    new = plan(tmp_path)
    assert old["partition_inputs"] != new["partition_inputs"]
    assert old["plan_id"] != new["plan_id"]
    path.write_bytes(b"invalid parquet")
    result = plan(tmp_path)
    assert result["status"] == "ERROR"
    assert result["counts"] == {"INPUT_UNREADABLE": 1}
    assert not result["rebuild_candidates"]


def test_unsafe_target_partition_does_not_propose_upsert(tmp_path):
    store, candles = seed(tmp_path)
    parent = aggregate_candle_series("BTCUSD", "1m", "1h", candles)[0]
    frame = CandleParquetStore._frame([parent], source="canonical_tick_lake", data_quality="ok", coverage_ok=True)
    frame["open_time"] = pd.to_datetime(frame.open_time) - pd.Timedelta(hours=1)
    frame = frame.drop(columns="is_closed")
    path = store.partition_path(parent)
    path.parent.mkdir(parents=True)
    frame.to_parquet(path)
    result = plan(tmp_path)
    assert result["counts"] == {"TARGET_PARTITION_UNSAFE": 1}
    assert result["unsafe_target_partitions"]["1h"] == ["2026-08.parquet"]


def test_report_isolates_symbol_errors(tmp_path):
    result = build_delta_recovery_report(tmp_path, tmp_path / "candles",
        symbols=("BTCUSD", "unsupported"), as_of=START)
    assert result["symbols"]["BTCUSD"]["status"] == "GAPS_REMAIN"
    assert result["symbols"]["unsupported"]["status"] == "ERROR"
    assert not result["can_apply"]
