from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.data.delta_lake_repair import repair_delta_lake
from vnedge.exchange.writer_lease import CanonicalWriterLease, CanonicalWriterLeaseError, INHERITED_WRITER_LEASE_FD

START = datetime(2026, 8, 31, tzinfo=UTC)


def test_atomic_preserves_shared_access(tmp_path):
    from vnedge.data.delta_lake_repair import _atomic
    path = tmp_path / "report.json"
    _atomic(path, b"{}")
    assert path.stat().st_mode & 0o777 == 0o644
    path.chmod(0o640)
    owner = (path.stat().st_uid, path.stat().st_gid)
    _atomic(path, b'{"updated":true}')
    assert path.stat().st_mode & 0o777 == 0o640
    assert (path.stat().st_uid, path.stat().st_gid) == owner


def fixtures(tmp_path):
    store = CandleParquetStore(tmp_path / "candles", exchange="delta_india")
    candles = [Candle(symbol="BTCUSD", timeframe="1m", open_time=START+timedelta(minutes=i),
                      close_time=START+timedelta(minutes=i+1), open=Decimal(100), high=Decimal(100),
                      low=Decimal(100), close=Decimal(100), volume=Decimal("0.01"),
                      quote_volume=Decimal(1), trade_count=1, taker_buy_volume=Decimal("0.01"),
                      vwap=Decimal(100)) for i in range(60)]
    store.upsert(candles)
    tape = tmp_path / "ticks/exchange=delta_india/symbol=BTCUSD/stream=trades/20260831/tape.parquet"
    tape.parent.mkdir(parents=True)
    pd.DataFrame([dict(ts_ms=int((START+timedelta(minutes=i,seconds=1)).timestamp()*1000),
                       price=100.,amount=10.,side="buy") for i in range(61)]).to_parquet(tape)
    return store, candles, tape


def run(tmp_path, apply=False, **kwargs):
    return repair_delta_lake(tmp_path, tmp_path/"candles", symbols=("BTCUSD",),
                              apply=apply, now=START+timedelta(days=3), **kwargs)["symbols"]["BTCUSD"]


def test_exact_tape_restores_only_metadata_and_backups(tmp_path):
    store, candles, tape = fixtures(tmp_path)
    path = store.partition_path(candles[0])
    old = pd.read_parquet(path).drop(columns="is_closed")
    old.to_parquet(path)
    before = path.read_bytes()
    audit = run(tmp_path)
    assert audit["closed_flags_restored"] == 60
    assert path.read_bytes() == before
    report = run(tmp_path, apply=True)
    assert report["closed_flags_restored"] == 60
    after = pd.read_parquet(path)
    pd.testing.assert_frame_equal(after.drop(columns="is_closed"),old)
    assert after.is_closed.all()
    assert next((tmp_path/"repairs/delta/backups").glob("*.parquet")).read_bytes() == before
    assert len(store.read("BTCUSD","1h")) == 1
    assert not report["arena_history_ready"]
    again = run(tmp_path, apply=True)
    assert again["closed_flags_restored"] == 0
    assert again["parents_inserted"] == 0


@pytest.mark.parametrize("bad", ["partial", "hash", "tape", "source"])
def test_bad_proof_not_upgraded(tmp_path,bad):
    store,candles,tape=fixtures(tmp_path)
    path=store.partition_path(candles[0])
    old=pd.read_parquet(path).drop(columns="is_closed")
    if bad=="partial":
        old["data_quality"]="partial"
        old["coverage_ok"]=False
    if bad=="hash": old["content_sha256"]="a"*64
    if bad=="source": old["source"]="official_delta_ohlc"
    if bad=="tape":
        ticks=pd.read_parquet(tape)
        ticks["amount"]=20.
        ticks.to_parquet(tape)
    old.to_parquet(path)
    before=path.read_bytes()
    report=run(tmp_path,apply=True)
    assert report["closed_flags_restored"]==0
    assert path.read_bytes()==before
    assert not store.read("BTCUSD","1h")


def test_partial_and_missing_minutes_cannot_create_parent(tmp_path):
    store,candles,_=fixtures(tmp_path)
    store.upsert([candles[2]],data_quality="partial",coverage_ok=False)
    path=store.partition_path(candles[0])
    pd.read_parquet(path).drop(index=3).to_parquet(path)
    report=run(tmp_path,apply=True)
    assert not store.read("BTCUSD","1h")
    assert report["levels"]["1m"]["missing_internal_slots"]==2
    assert report["unresolved"]["1m"]==1


def test_active_owner_requires_inherited_authority(tmp_path):
    fixtures(tmp_path)
    with CanonicalWriterLease(tmp_path,"delta_india") as lease:
        with pytest.raises(CanonicalWriterLeaseError):
            run(tmp_path,apply=True)
        report=run(tmp_path,apply=True,environ={INHERITED_WRITER_LEASE_FD:str(lease.fileno)})
        assert report["parents_inserted"]>0


def test_legacy_contract_units_reconstructed_but_never_promoted(tmp_path):
    store,candles,_=fixtures(tmp_path)
    path=store.partition_path(candles[0])
    old=pd.read_parquet(path).drop(columns=["is_closed","source","content_sha256","data_quality","coverage_ok","parent_open"])
    for k in ("volume","quote_volume","taker_buy_volume"):
        old[k] = old[k].map(lambda x: x*1000)
    old.to_parquet(path)
    report=run(tmp_path,apply=True)
    assert report["legacy_units_rebuilt_partial"]==60
    after=pd.read_parquet(path)
    assert after.volume.iloc[0]==Decimal("0.01")
    assert after.is_closed.all()
    assert (after.source=="repaired").all()
    assert (after.data_quality=="partial").all()
    assert not after.coverage_ok.any()
    assert not store.read("BTCUSD","1h")
    assert not report["arena_history_ready"]
