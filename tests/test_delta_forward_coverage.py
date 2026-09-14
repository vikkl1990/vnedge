import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from vnedge.data.candles import CandleBuilder, CandleParquetStore
from vnedge.data.delta_coverage import (
    ForwardCoverage,
    candle_hash,
    read_coverage_journal,
    replay_sealed_minute,
    reproduce_sealed_minutes,
)
from vnedge.data.delta_raw_audit import audit_raw_day, load_raw_shards
from vnedge.exchange.tick_recorder import DeltaTickRecorder, _Buffer
from vnedge.exchange.writer_lease import (
    INHERITED_WRITER_LEASE_FD,
    CanonicalWriterLease,
    CanonicalWriterLeaseError,
)

START = datetime(2026, 9, 1, tzinfo=UTC)
CLOSE = START + timedelta(minutes=1)


def raw_row(i=1, **extra):
    return {
        "ts_ms": int((START + timedelta(seconds=i)).timestamp() * 1000),
        "price": 100.0,
        "size_contracts": 3.0,
        "amount": 3.0,
        "contract_value": 0.001,
        "base_amount": 0.003,
        "side": "buy",
        "trade_id": str(i),
        "exchange_timestamped": True,
        "received_ts_ms": int(START.timestamp() * 1000) + i * 1000 + 2,
        **extra,
    }


def fixture(tmp_path, *, seal=True, rows=None):
    rows = rows or [raw_row(), raw_row(2)]
    proof = ForwardCoverage(
        tmp_path, tmp_path / "candles", "BTCUSD", started_at=START - timedelta(minutes=1)
    )
    proof.connection(True, START - timedelta(minutes=1))
    buf = _Buffer(
        tmp_path, "delta_india", "BTCUSD", "trades", on_shard=proof.shard, shard_suffix="-test"
    )
    builder = CandleBuilder("BTCUSD", "1m")
    for row in rows:
        buf.add(row)
        builder.on_trade(
            datetime.fromtimestamp(row["ts_ms"] / 1000, tz=UTC),
            Decimal(str(row["price"])),
            Decimal(str(row["base_amount"])),
            False,
        )
    buf.flush(1)
    candle = builder.close_if_elapsed(CLOSE)
    store = CandleParquetStore(tmp_path / "candles", exchange="delta_india")
    store.upsert([candle])
    if seal:
        proof.published(candle)
        proof.checkpoint(int(CLOSE.timestamp() * 1000), {}, at=CLOSE + timedelta(seconds=1))
    return proof, candle, store


def sealed(proof):
    records = read_coverage_journal(proof.path)
    event = next(r for r in records if r["kind"] == "minute_sealed")
    return records, event


def test_forward_seal_replays_exact_original_and_binds_raw_bytes(tmp_path):
    proof, candle, _ = fixture(tmp_path)
    records, event = sealed(proof)
    replay = replay_sealed_minute(tmp_path, records, event["record_hash"])
    assert candle_hash(replay) == candle_hash(candle)
    assert event["payload"]["venue_completeness"] == "UNPROVEN"
    shard = tmp_path / event["payload"]["shards"][0]["path"]
    pd.read_parquet(shard).assign(price=102.0).to_parquet(shard)
    with pytest.raises(ValueError, match="shard_hash_mismatch"):
        replay_sealed_minute(tmp_path, records, event["record_hash"])


def test_owner_restores_only_missing_original_and_is_idempotent(tmp_path):
    _, candle, store = fixture(tmp_path)
    path = store.partition_path(candle)
    path.unlink()
    audit = reproduce_sealed_minutes(tmp_path, tmp_path / "candles", symbol="BTCUSD", now=CLOSE)
    assert audit["candidate_minutes"] == 1 and audit["restored_minutes"] == 0
    assert not path.exists()
    result = reproduce_sealed_minutes(
        tmp_path, tmp_path / "candles", symbol="BTCUSD", now=CLOSE, apply=True
    )
    assert result["restored_minutes"] == 1
    assert store.read_records("BTCUSD", "1m")[0].content_sha256 == candle_hash(candle)
    again = reproduce_sealed_minutes(
        tmp_path, tmp_path / "candles", symbol="BTCUSD", now=CLOSE, apply=True
    )
    assert again["restored_minutes"] == 0 and again["already_present"] == 1


def test_reproduction_requires_the_existing_writer_lease(tmp_path):
    _, candle, store = fixture(tmp_path)
    store.partition_path(candle).unlink()
    with CanonicalWriterLease(tmp_path, "delta_india") as lease:
        with pytest.raises(CanonicalWriterLeaseError):
            reproduce_sealed_minutes(
                tmp_path, tmp_path / "candles", symbol="BTCUSD", now=CLOSE, apply=True
            )
        result = reproduce_sealed_minutes(
            tmp_path,
            tmp_path / "candles",
            symbol="BTCUSD",
            now=CLOSE,
            apply=True,
            environ={INHERITED_WRITER_LEASE_FD: str(lease.fileno)},
        )
        assert result["restored_minutes"] == 1


@pytest.mark.parametrize("fault", ["reconnect", "input_fault", "restart", "hash", "forming_flag"])
def test_incomplete_or_nonpersisted_intervals_never_seal(tmp_path, fault):
    proof, candle, store = fixture(tmp_path, seal=False)
    if fault == "reconnect":
        proof.connection(False, START + timedelta(seconds=20))
        proof.connection(True, START + timedelta(seconds=30))
    if fault == "input_fault":
        proof.fault("dropped", START + timedelta(seconds=30))
    if fault == "restart":
        proof.started_at = START + timedelta(seconds=30)
    if fault in {"hash", "forming_flag"}:
        path = store.partition_path(candle)
        frame = pd.read_parquet(path)
        if fault == "hash":
            frame["content_sha256"] = "a" * 64
        else:
            frame["is_closed"] = False
        frame.to_parquet(path)
    proof.published(candle)
    proof.checkpoint(int(CLOSE.timestamp() * 1000), {}, at=CLOSE + timedelta(seconds=1))
    assert not any(r["kind"] == "minute_sealed" for r in read_coverage_journal(proof.path))


def test_chain_corruption_and_tear_cannot_authorize_repair(tmp_path):
    proof, candle, store = fixture(tmp_path)
    store.partition_path(candle).unlink()
    data = proof.path.read_text().replace('"seq": 1', '"seq": 99')
    proof.path.write_text(data)
    with pytest.raises(ValueError, match="chain_invalid"):
        read_coverage_journal(proof.path)
    result = reproduce_sealed_minutes(
        tmp_path, tmp_path / "candles", symbol="BTCUSD", now=CLOSE, apply=True
    )
    assert result["restored_minutes"] == 0 and result["rejected"]


def test_existing_partial_bar_is_not_overwritten(tmp_path):
    _, candle, store = fixture(tmp_path)
    store.upsert([candle], coverage_ok=False)
    path = store.partition_path(candle)
    before = path.read_bytes()
    result = reproduce_sealed_minutes(
        tmp_path, tmp_path / "candles", symbol="BTCUSD", now=CLOSE, apply=True
    )
    assert result["restored_minutes"] == 0 and path.read_bytes() == before


def test_replay_requires_original_hash_not_just_dense_raw(tmp_path):
    proof, _, _ = fixture(tmp_path, rows=[raw_row(), raw_row(2, price=101.0)])
    records, event = sealed(proof)
    event["payload"]["bar_hash"] = "a" * 64
    with pytest.raises(ValueError, match="original_hash_mismatch"):
        replay_sealed_minute(tmp_path, records, event["record_hash"])


@pytest.mark.parametrize(
    "extra,counter",
    [
        ({"contract_value": 1.0}, "invalid_rows"),
        ({"size_contracts": 2.5}, "invalid_rows"),
        ({"exchange_timestamped": False}, "exchange_time_unproven"),
        ({"base_amount": None}, "legacy_units_unproven"),
    ],
)
def test_raw_audit_discloses_bad_units_and_time(tmp_path, extra, counter):
    buf = _Buffer(tmp_path, "delta_india", "BTCUSD", "trades")
    buf.add(raw_row(**extra))
    buf.flush(1)
    report = audit_raw_day(tmp_path, "BTCUSD", "20260901")
    assert report["counts"][counter] == 1
    assert report["coverage"] == "UNPROVEN" and not report["can_replay_as_canonical"]


def test_raw_identified_duplicates_and_idless_twins_are_not_silently_deduped(tmp_path):
    buf = _Buffer(tmp_path, "delta_india", "BTCUSD", "trades")
    for row in [
        raw_row(),
        raw_row(),
        raw_row(price=101),
        raw_row(2, trade_id=None),
        raw_row(2, trade_id=None),
    ]:
        buf.add(row)
    buf.flush(1)
    report = audit_raw_day(tmp_path, "BTCUSD", "20260901")
    assert report["counts"]["duplicate_ids"] == 1
    assert report["counts"]["conflicting_ids"] == 1
    assert report["counts"]["identical_idless_rows"] == 1
    assert report["valid_rows"] == 5


def test_timestamp_gaps_and_missing_files_never_claim_completeness(tmp_path):
    buf = _Buffer(tmp_path, "delta_india", "BTCUSD", "trades")
    buf.add(raw_row())
    buf.add(raw_row(100))
    buf.flush(1)
    report = audit_raw_day(tmp_path, "BTCUSD", "20260901")
    assert report["max_interprint_gap_ms"] == 99_000
    assert report["coverage"] == "UNPROVEN"
    assert audit_raw_day(tmp_path, "BTCUSD", "20260902")["status"] == "NO_LOCAL_SHARDS"


def test_unreadable_and_escaping_shard_rejected(tmp_path):
    proof, _, _ = fixture(tmp_path)
    _, event = sealed(proof)
    relative = event["payload"]["shards"][0]["path"]
    (tmp_path / relative).write_bytes(b"broken")
    assert audit_raw_day(tmp_path, "BTCUSD", "20260901")["status"] == "ERROR"
    with pytest.raises(ValueError, match="outside_symbol"):
        load_raw_shards(tmp_path, "BTCUSD", ["../secret.parquet"], start=START, end=CLOSE)


def test_delta_restart_shards_have_distinct_names(tmp_path):
    recorders = [DeltaTickRecorder(["BTCUSD"], tmp_path) for _ in range(2)]
    for rec in recorders:
        rec._trade_bufs["BTCUSD"].add(raw_row())
        rec._trade_bufs["BTCUSD"].flush(1)
    assert len(list(tmp_path.rglob("*.parquet"))) == 2


def test_coverage_write_failure_disables_seals_not_the_candle_path(tmp_path, monkeypatch):
    proof, candle, _ = fixture(tmp_path, seal=False)

    def broken(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("vnedge.data.delta_coverage.os.fsync", broken)
    proof.fault("test", START)
    assert not proof.healthy
    proof.published(candle)
    assert not proof.pending


def test_parser_faults_reach_coverage_without_new_fire_logic(tmp_path):
    rec = DeltaTickRecorder(["BTCUSD"], tmp_path)
    faults = []
    rec._client.on_trade_fault = lambda symbol, reason: faults.append((symbol, reason))
    rec._client._handle_trade("BTCUSD", {"p": "broken", "s": 1})
    rec._client._handle_trade("BTCUSD", {"p": "100", "s": 1, "t": "broken"})
    assert faults == [("BTCUSD", "trade_parse_rejected"), ("BTCUSD", "trade_timestamp_rejected")]


def test_real_recorder_publishes_persists_and_seals_same_minute(tmp_path):
    rec = DeltaTickRecorder(["BTCUSD"], tmp_path, candle_root=tmp_path / "candles")
    proof = ForwardCoverage(
        tmp_path, tmp_path / "candles", "BTCUSD", started_at=START - timedelta(minutes=1)
    )
    rec._coverage["BTCUSD"] = proof
    rec.candle_sink.trade_coverage["BTCUSD"].gap_start = START - timedelta(minutes=1)
    rec._on_trade_connection_state(True, START - timedelta(minutes=1))
    for i in (1, 2):
        rec._client._handle_trade(
            "BTCUSD", {"p": "100", "s": 3, "r": "m", "t": raw_row(i)["ts_ms"] * 1000, "id": str(i)}
        )
    rec._drain_delta_reorder("BTCUSD", through_ms=int(CLOSE.timestamp() * 1000))
    rec._trade_bufs["BTCUSD"].flush(1)
    rec.candle_sink.advance_time(CLOSE)
    proof.checkpoint(
        int(CLOSE.timestamp() * 1000), rec.trade_metrics_snapshot()["BTCUSD"], at=CLOSE
    )
    records, event = sealed(proof)
    replay = replay_sealed_minute(tmp_path, records, event["record_hash"])
    stored = rec.candle_sink.pipelines["BTCUSD"].store.read_records("BTCUSD", "1m")
    assert len(stored) == 1 and stored[0].content_sha256 == candle_hash(replay)
    assert replay.volume == Decimal("0.006") and replay.trade_count == 2


def test_fault_flood_is_counted_without_per_print_fsync(tmp_path):
    proof, candle, _ = fixture(tmp_path, seal=False)
    for i in range(1, 1001):
        proof.fault("normalized_trade_rejected", START + timedelta(milliseconds=i))
    proof.published(candle)
    proof.checkpoint(int(CLOSE.timestamp() * 1000), {}, at=CLOSE)
    records = read_coverage_journal(proof.path)
    assert sum(r["kind"] == "input_fault" for r in records) == 1
    assert not any(r["kind"] == "minute_sealed" for r in records)
    assert records[-1]["payload"]["input_fault_counts"]["normalized_trade_rejected"] == 1000


def test_torn_tail_and_raw_budget_reject(tmp_path, monkeypatch):
    proof, _, _ = fixture(tmp_path)
    proof.path.write_bytes(proof.path.read_bytes() + b'{"seq":')
    with pytest.raises(json.JSONDecodeError):
        read_coverage_journal(proof.path)
    monkeypatch.setattr("vnedge.data.delta_raw_audit.MAX_ROWS", 1)
    report = audit_raw_day(tmp_path, "BTCUSD", "20260901")
    assert report["status"] == "ERROR" and "row_budget_exceeded" in report["reason"]
    assert not report["can_replay_as_canonical"]


def test_reproduction_does_not_upgrade_legacy_target_partition(tmp_path):
    _, candle, store = fixture(tmp_path)
    path = store.partition_path(candle)
    # A different legacy row shares this daily partition. It must not acquire
    # provenance merely because a missing sealed minute could be restored.
    frame = pd.read_parquet(path).drop(columns=["source"])
    frame["open_time"] = pd.to_datetime(frame.open_time, utc=True) + timedelta(minutes=1)
    frame.to_parquet(path)
    before = path.read_bytes()
    report = reproduce_sealed_minutes(
        tmp_path, tmp_path / "candles", symbol="BTCUSD", now=CLOSE, apply=True
    )
    assert report["restored_minutes"] == 0 and "unsafe_target_partition" in report["rejected"][0]
    assert path.read_bytes() == before
