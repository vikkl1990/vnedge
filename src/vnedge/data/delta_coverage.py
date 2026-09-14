"""Forward-only recorder evidence, never a certificate for unseen venue trades.

A seal names an original published AND persisted closed minute, its exact raw
shards, and the observed connection/watermark boundary. Only byte-identical
reconstruction of that original is permitted; no historical coverage upgrade.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import Counter, deque
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from vnedge.data.candles import (
    CANDLE_STORAGE_COLUMNS,
    Candle,
    CandleBuilder,
    CandleParquetStore,
    _exclusive_lock,
)
from vnedge.data.delta_raw_audit import load_raw_shards
from vnedge.data.delta_recovery_plan import verified_candle
from vnedge.data.symbols import canonical_symbol
from vnedge.exchange.writer_lease import canonical_write_authority

logger = logging.getLogger(__name__)
MAX_RECORDS = 100_000
MAX_JOURNAL_BYTES = 64 * 1024 * 1024


def digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def candle_hash(candle: Candle) -> str:
    return str(
        CandleParquetStore._frame(
            [candle], source="canonical_tick_lake", data_quality="ok", coverage_ok=True
        )
        .iloc[0]
        .content_sha256
    )


class ForwardCoverage:
    def __init__(self, root: Path, candle_root: Path, symbol: str, *, started_at: datetime) -> None:
        if started_at.tzinfo is None:
            raise ValueError("aware_session_start_required")
        self.root, self.symbol = root, symbol
        self.store = CandleParquetStore(candle_root, exchange="delta_india")
        self.started_at = started_at.astimezone(UTC)
        self.session_id = uuid4().hex
        self.path = root / "coverage/delta_india" / symbol / f"{self.session_id}.jsonl"
        self.seq = 0
        self.previous = ""
        self.healthy = True
        self.connected_since: datetime | None = None
        self.pending: deque[Candle] = deque()
        self.shards: deque[dict[str, Any]] = deque()
        self.last_checkpoint: int | None = None
        self.last_fault_minute: int | None = None
        self.fault_counts: Counter[str] = Counter()
        self._record(
            "session_started",
            {
                "started_at": self.started_at.isoformat(),
                "venue_completeness": "UNPROVEN_NO_TRADE_SEQUENCE",
                "unit_contract": "delta_contracts_to_base_v1",
            },
        )

    def _record(self, kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.healthy:
            return None
        record = {
            "schema_version": 1,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "seq": self.seq,
            "previous_hash": self.previous,
            "kind": kind,
            "payload": payload,
        }
        try:
            record["record_hash"] = digest(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.seq >= MAX_RECORDS or (
                self.path.exists() and self.path.stat().st_size >= MAX_JOURNAL_BYTES
            ):
                raise ValueError("coverage_journal_budget_exceeded")
            with self.path.open("xb" if self.seq == 0 else "ab") as handle:
                handle.write((json.dumps(record, sort_keys=True, allow_nan=False) + "\n").encode())
                handle.flush()
                os.fsync(handle.fileno())
            if self.seq == 0:
                fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except Exception:
            self.healthy = False
            logger.exception("Delta forward coverage unavailable; no further replay seals")
            return None
        self.seq += 1
        self.previous = record["record_hash"]
        return record

    def connection(self, connected: bool, at: datetime) -> None:
        at = at.astimezone(UTC)
        self.connected_since = at if connected else None
        self._record("connected" if connected else "disconnected", {"at": at.isoformat()})

    def fault(self, reason: str, at: datetime) -> None:
        # A dropped/malformed/conflicting print invalidates the current minute,
        # not only the print's timestamp (which may itself be malformed).
        if self.connected_since is not None:
            self.connected_since = at.astimezone(UTC)
        self.fault_counts[reason] += 1
        minute = int(at.timestamp()) // 60
        if minute != self.last_fault_minute:
            self._record(
                "input_fault",
                {
                    "at": at.isoformat(),
                    "reason": reason,
                    "coalescing": "first_fault_per_receipt_minute; all faults invalidate sealing",
                },
            )
            self.last_fault_minute = minute

    def shard(self, path: Path, first_ms: int, last_ms: int) -> None:
        if not self.healthy:
            return
        try:
            self.shards.append(
                {
                    "path": str(path.relative_to(self.root)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "first_ms": first_ms,
                    "last_ms": last_ms,
                }
            )
            if len(self.shards) > 256:
                raise ValueError("forward_shard_budget_exceeded")
        except Exception:
            self.healthy = False
            logger.exception("Delta coverage shard binding failed; no replay seals")

    def published(self, candle: Candle) -> None:
        if candle.timeframe != "1m" or not self.healthy:
            return
        if len(self.pending) >= 8:
            self.healthy = False
            logger.error("Delta coverage pending overflow; no replay seals")
            return
        self.pending.append(candle)

    def checkpoint(self, watermark_ms: int, metrics: dict[str, Any], *, at: datetime) -> None:
        if not self.healthy:
            return
        minute = watermark_ms // 60_000
        if minute != self.last_checkpoint:
            self._record(
                "watermark",
                {
                    "at": at.isoformat(),
                    "watermark_ms": watermark_ms,
                    "metrics": metrics,
                    "input_fault_counts": dict(self.fault_counts),
                    "queue_policy": "unbounded_reorder_heap",
                    "overflow_drops": None,
                },
            )
            self.last_checkpoint = minute
        while self.pending and int(self.pending[0].close_time.timestamp() * 1000) <= watermark_ms:
            candle = self.pending.popleft()
            lo, hi = (
                int(candle.open_time.timestamp() * 1000),
                int(candle.close_time.timestamp() * 1000),
            )
            if (
                self.connected_since is None
                or candle.open_time < max(self.started_at, self.connected_since)
                or candle.trade_count <= 0
            ):
                continue
            try:
                # Subscriber delivery happens before Parquet persistence. Seal
                # only now, after verifying the persisted row independently.
                frame = pd.read_parquet(self.store.partition_path(candle))
                rows = frame.loc[pd.to_datetime(frame.open_time, utc=True) == candle.open_time]
                if len(rows) != 1:
                    raise ValueError("persisted_slot_not_unique")
                row = rows.iloc[0].to_dict()
                verified_candle(row, self.symbol, "1m", at)
                if row["content_sha256"] != candle_hash(candle):
                    raise ValueError("published_persisted_hash_mismatch")
                shards = [s for s in self.shards if s["first_ms"] < hi and s["last_ms"] >= lo]
                if not shards:
                    raise ValueError("no_bound_raw_shards")
                self._record(
                    "minute_sealed",
                    {
                        "open_time": candle.open_time.isoformat(),
                        "close_time": candle.close_time.isoformat(),
                        "sealed_at": at.isoformat(),
                        "connected_since": self.connected_since.isoformat(),
                        "watermark_ms": watermark_ms,
                        "bar_hash": row["content_sha256"],
                        "shards": shards,
                        "scope": "original_published_bar_reproduction_only",
                        "venue_completeness": "UNPROVEN",
                    },
                )
            except Exception as exc:
                logger.exception("Delta original minute could not be sealed")
                self._record(
                    "seal_rejected",
                    {
                        "open_time": candle.open_time.isoformat(),
                        "reason": f"{type(exc).__name__}:{exc}",
                    },
                )
        # Preserve enough boundary overlap for a delayed close; a resource limit
        # disables new proof instead of silently dropping required evidence.
        cutoff = watermark_ms - 180_000
        self.shards = deque(s for s in self.shards if s["last_ms"] >= cutoff)


def read_coverage_journal(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > MAX_JOURNAL_BYTES:
        raise ValueError("coverage_journal_byte_budget")
    records = []
    previous = ""
    for line in path.read_bytes().splitlines():
        record = json.loads(line)
        hashed = dict(record)
        claimed = hashed.pop("record_hash")
        if (
            len(records) >= MAX_RECORDS
            or record["seq"] != len(records)
            or record["previous_hash"] != previous
            or digest(hashed) != claimed
        ):
            raise ValueError("coverage_chain_invalid")
        if record["schema_version"] != 1 or (
            records
            and (record["session_id"], record["symbol"])
            != (records[0]["session_id"], records[0]["symbol"])
        ):
            raise ValueError("coverage_identity_invalid")
        previous = claimed
        records.append(record)
    if not records or records[0]["kind"] != "session_started":
        raise ValueError("coverage_session_missing")
    return records


def replay_sealed_minute(root: Path, records: list[dict[str, Any]], record_hash: str) -> Candle:
    """Caller supplies the fully verified session chain; no free-form manifest."""
    target = next((r for r in records if r["record_hash"] == record_hash), None)
    if target is None or target["kind"] != "minute_sealed":
        raise ValueError("sealed_minute_missing")
    p = target["payload"]
    opened, closed = (
        pd.Timestamp(p["open_time"]).to_pydatetime(),
        pd.Timestamp(p["close_time"]).to_pydatetime(),
    )
    started = pd.Timestamp(records[0]["payload"]["started_at"]).to_pydatetime()
    connected: datetime | None = None
    for event in records[1 : target["seq"]]:
        if event["kind"] in {"connected", "input_fault"}:
            if event["kind"] == "connected" or connected is not None:
                connected = pd.Timestamp(event["payload"]["at"]).to_pydatetime()
        elif event["kind"] == "disconnected":
            connected = None
    if (
        opened.tzinfo is None
        or opened.second
        or opened.microsecond
        or closed.timestamp() - opened.timestamp() != 60
        or connected is None
        or opened < max(started, connected)
    ):
        raise ValueError("interval_not_forward_covered")
    if p["watermark_ms"] < int(closed.timestamp() * 1000) or pd.Timestamp(p["sealed_at"]) < closed:
        raise ValueError("seal_before_watermark")
    shards = p["shards"]
    rows, audit = load_raw_shards(
        root,
        target["symbol"],
        [s["path"] for s in shards],
        start=opened,
        end=closed,
        expected_hashes={s["path"]: s["sha256"] for s in shards},
    )
    counts = audit["counts"]
    if not rows or any(
        counts.get(k, 0)
        for k in (
            "legacy_units_unproven",
            "exchange_time_unproven",
            "invalid_rows",
            "duplicate_ids",
            "conflicting_ids",
        )
    ):
        raise ValueError("raw_rows_unproven_or_ambiguous")
    builder = CandleBuilder(target["symbol"], "1m")
    for ts, price, base, side in rows:
        builder.on_trade(datetime.fromtimestamp(ts / 1000, tz=UTC), price, base, side == "sell")
    candle = builder.close_if_elapsed(closed)
    if candle is None or candle_hash(candle) != p["bar_hash"]:
        raise ValueError("replay_original_hash_mismatch")
    return candle


def reproduce_sealed_minutes(
    root: Path,
    candle_root: Path,
    *,
    symbol: str,
    apply: bool = False,
    environ: Any = None,
    now: datetime | None = None,
    max_minutes: int = 32,
) -> dict[str, Any]:
    """Restore missing original minutes only, under the existing owner lease.

    At most eight recent sessions / seven days / 32 missing slots per cycle.
    Existing slots (even invalid ones) are never overwritten or re-attested.
    """
    symbol = canonical_symbol(symbol)
    if symbol not in {"BTCUSD", "ETHUSD"} or not 1 <= max_minutes <= 32:
        raise ValueError("invalid_reproduction_scope")
    now = now or datetime.now(UTC)
    report: dict[str, Any] = {
        "symbol": symbol,
        "mode": "apply" if apply else "audit",
        "candidate_minutes": 0,
        "restored_minutes": 0,
        "already_present": 0,
        "rejected": [],
        "proof_ids": [],
        "scope": "original_published_minutes_only",
        "can_trade": False,
        "can_promote": False,
    }
    files = sorted(
        (root / "coverage/delta_india" / symbol).glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    report["sessions_discovered"] = len(files)
    report["sessions_inspected"] = min(len(files), 8)
    store = CandleParquetStore(candle_root, exchange="delta_india")
    seen: set[datetime] = set()
    # Cache presence only for this maintenance pass, not across cycles. Without
    # this, a healthy week causes thousands of whole-day Parquet reads. Every
    # proposed write still rereads the partition under its lock below.
    present: dict[Path, set[datetime]] = {}
    attempts = 0
    authority = (
        canonical_write_authority(
            root, "delta_india", environ=os.environ if environ is None else environ
        )
        if apply
        else nullcontext()
    )
    with authority:
        for journal in files[:8]:
            try:
                records = read_coverage_journal(journal)
                if records[0]["symbol"] != symbol:
                    raise ValueError("journal_symbol_mismatch")
            except Exception as exc:
                logger.exception("Delta coverage journal rejected")
                report["rejected"].append(f"{journal.name}:{type(exc).__name__}:{exc}")
                continue
            for event in records:
                if event["kind"] != "minute_sealed":
                    continue
                p = event["payload"]
                try:
                    opened = pd.Timestamp(p["open_time"]).to_pydatetime()
                    closed = pd.Timestamp(p["close_time"]).to_pydatetime()
                    if opened.tzinfo is None or closed.tzinfo is None:
                        raise ValueError("naive_seal_interval")
                except (ValueError, TypeError, KeyError, OverflowError) as exc:
                    report["rejected"].append(f"{event['record_hash']}:{type(exc).__name__}:{exc}")
                    continue
                if opened in seen or opened < now - timedelta(days=7) or closed > now:
                    continue
                seen.add(opened)
                # Minute storage is daily. Inspect without rewriting/normalizing.
                path = (
                    candle_root
                    / "exchange=delta_india"
                    / symbol
                    / "1m"
                    / f"{opened:%Y-%m-%d}.parquet"
                )
                try:
                    if path not in present:
                        if path.exists() and path.stat().st_size > 128 * 1024 * 1024:
                            raise ValueError("target_partition_byte_budget")
                        old = (
                            pd.read_parquet(path, columns=["open_time"]) if path.exists() else None
                        )
                        present[path] = (
                            set(pd.to_datetime(old.open_time, utc=True))
                            if old is not None
                            else set()
                        )
                    if opened in present[path]:
                        report["already_present"] += 1
                        continue
                    if attempts >= max_minutes:
                        report["budget_exhausted"] = True
                        return report
                    attempts += 1
                    candle = replay_sealed_minute(root, records, event["record_hash"])
                    with (
                        _exclusive_lock(path.with_suffix(".parquet.lock"))
                        if apply
                        else nullcontext()
                    ):
                        # Recorder may have written since the initial read.
                        if path.exists() and path.stat().st_size > 128 * 1024 * 1024:
                            raise ValueError("target_partition_byte_budget")
                        old = pd.read_parquet(path) if path.exists() else None
                        if old is not None:
                            if (pd.to_datetime(old.open_time, utc=True) == opened).any():
                                report["already_present"] += 1
                                continue
                            if set(old.columns) != set(CANDLE_STORAGE_COLUMNS) or any(
                                old[k].isna().any()
                                for k in (
                                    "is_closed",
                                    "source",
                                    "content_sha256",
                                    "data_quality",
                                    "coverage_ok",
                                )
                            ):
                                raise ValueError("unsafe_target_partition")
                        report["candidate_minutes"] += 1
                        report["proof_ids"].append(event["record_hash"])
                        if apply:
                            new = store._frame(
                                [candle],
                                source="canonical_tick_lake",
                                data_quality="ok",
                                coverage_ok=True,
                            )
                            combined = (
                                pd.concat([old, new], ignore_index=True) if old is not None else new
                            )
                            store._write_atomic(
                                path, combined.sort_values("open_time").reset_index(drop=True)
                            )
                            report["restored_minutes"] += 1
                            present[path].add(opened)
                except Exception as exc:
                    logger.exception("Delta original minute reproduction rejected")
                    report["rejected"].append(f"{event['record_hash']}:{type(exc).__name__}:{exc}")
    return report
