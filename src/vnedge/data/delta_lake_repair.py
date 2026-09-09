"""Bounded, owner-authorized Delta repair; never attest an incomplete tape.

Only restore a legacy closed flag when independently replayed tape matches an
already persisted canonical hash AND coverage/quality attestation. Missing or
partial minutes require separate completeness evidence; their mere presence
in a raw shard is not that evidence. Complete verified children may repair a
missing parent. Historical raw inventory is reported, not silently promoted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd

from vnedge.data.candle_bootstrap import _rows
from vnedge.data.candles import (
    Candle, CandleBuilder, CandleParquetStore, _decision_hash_row, _exclusive_lock,
    aggregate_candle_series,
)
from vnedge.data.bar_identity import bar_content_sha256
from vnedge.data.symbols import canonical_symbol
from vnedge.exchange.delta_snapshot_validation import BASELINE
from vnedge.exchange.writer_lease import canonical_write_authority

LADDER = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
        temporary = f.name
    os.replace(temporary, path)


def _hash(candle: Candle) -> str:
    row = CandleParquetStore._frame([candle], source="canonical_tick_lake",
                                   data_quality="ok", coverage_ok=True).iloc[0]
    return str(row.content_sha256)


def _attested(row: dict[str, Any]) -> bool:
    """This does NOT supply absent provenance or make a partial bar whole."""
    if row.get("source") != "canonical_tick_lake" or row.get("data_quality") != "ok":
        return False
    if pd.isna(row.get("coverage_ok")) or row.get("coverage_ok") != True:
        return False
    try:
        return row.get("content_sha256") == bar_content_sha256(
            _decision_hash_row(row), open_time=pd.Timestamp(row["open_time"]).to_pydatetime(),
            close_time=pd.Timestamp(row["close_time"]).to_pydatetime(), source="canonical_tick_lake")
    except (ValueError, TypeError, KeyError):
        return False


def replay_day(root: Path, symbol: str, day: str) -> dict[tuple[str, datetime], Candle]:
    """Replay a CLOSED UTC day with adjacent boundary prints, bounded to 2M rows.

Stable global timestamp ordering across shards, no equal-print deduplication:
identical prints can be genuine trades. Any duplication changes the resulting
hash and prevents metadata repair. No coverage is inferred for new bars.
"""
    started = datetime.strptime(day, "%Y%m%d").replace(tzinfo=UTC)
    end = started + timedelta(days=1)
    tape = root / "ticks/exchange=delta_india" / f"symbol={symbol}" / "stream=trades"
    rows = []
    import pyarrow.parquet as pq
    paths = [p for offset in (-1, 0, 1)
             for p in sorted((tape / (started + timedelta(days=offset)).strftime("%Y%m%d")).glob("*.parquet"))]
    if sum(pq.ParquetFile(p).metadata.num_rows for p in paths) > 2_000_000:
        raise ValueError("raw_replay_row_budget_exceeded")
    for path in paths:
        rows.extend(r for r in _rows(path) if int((started-timedelta(minutes=1)).timestamp()*1000)
                    <= r[0] <= int((end+timedelta(minutes=1)).timestamp()*1000))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return {}
    builder = CandleBuilder(symbol, "1m")
    candles: list[Candle] = []
    multiplier = Decimal(str(BASELINE[symbol]["contract_value"]))
    for ts, price, amount, side in rows:
        contracts = Decimal(str(amount))
        if not contracts.is_finite() or contracts <= 0 or contracts != contracts.to_integral_value():
            raise ValueError("invalid_delta_contract_count")
        candle = builder.on_trade(datetime.fromtimestamp(ts/1000, tz=UTC), Decimal(str(price)),
                                  contracts * multiplier, False if side == "buy" else True if side == "sell" else None)
        if candle and started <= candle.open_time < end:
            candles.append(candle)
    result = {("1m", c.open_time): c for c in candles}
    for source, target in zip(LADDER, LADDER[1:]):
        candles = list(aggregate_candle_series(symbol, source, target, candles))
        result.update({(target, c.open_time): c for c in candles})
    return result


def _repair_delta_lake(
    data_root: Path, candle_root: Path, *, symbols: tuple[str, ...],
    apply: bool = False, max_replay_days: int = 2,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    report: dict[str, Any] = {"schema_version": 1, "generated_at": now.isoformat(),
                            "mode": "apply" if apply else "audit", "symbols": {},
                            "can_trade": False, "coverage_invented": False}
    for raw_symbol in symbols:
        symbol = canonical_symbol(raw_symbol)
        if symbol not in {"BTCUSD", "ETHUSD"}:
            raise ValueError("unsupported_delta_repair_symbol")
        store = CandleParquetStore(candle_root, exchange="delta_india")
        directory = candle_root / "exchange=delta_india" / symbol
        detail: dict[str, Any] = {"closed_flags_restored": 0, "legacy_units_rebuilt_partial": 0,
                                  "parents_inserted": 0,
                                  "unresolved": {}, "replay_errors": [], "levels": {}}
        tape_dir = data_root / "ticks/exchange=delta_india" / f"symbol={symbol}" / "stream=trades"
        days = sorted(p.name for p in tape_dir.glob("*") if p.is_dir() and len(p.name) == 8 and p.name.isdigit())
        detail["raw_days"] = len(days)
        detail["raw_first_day"] = days[0] if days else None
        detail["historical_coverage"] = "unproven_no_completeness_manifest"
        replayed: dict[str, dict[tuple[str, datetime], Candle]] = {}
        def replay_candidate(tf: str, opened: datetime) -> Candle | None:
            day = opened.strftime("%Y%m%d")
            if day >= now.strftime("%Y%m%d"):
                return None
            if day not in replayed and len(replayed) < max_replay_days:
                try:
                    replayed[day] = replay_day(data_root, symbol, day)
                except Exception as exc:
                    detail["replay_errors"].append(f"{day}:{type(exc).__name__}:{exc}")
                    replayed[day] = {}
            return replayed.get(day, {}).get((tf, opened))

        verified: dict[str, list[Candle]] = {}
        for tf in LADDER:
            verified[tf] = []
            present: set[datetime] = set()
            for path in sorted((directory / tf).glob("*.parquet")):
                # Serialize against the recorder's same partition lock. Raw
                # replay occurs only on closed historical days, never today.
                with _exclusive_lock(path.with_suffix(".parquet.lock")):
                    before = path.read_bytes()
                    frame = pd.read_parquet(path)
                    changed = False
                    records = frame.to_dict("records")
                    for idx, row in enumerate(records):
                        opened = pd.Timestamp(row["open_time"]).to_pydatetime()
                        present.add(opened)
                        if not _attested(row):
                            # Pre-provenance legacy rows can contain contracts
                            # in the coin-volume columns. Verify the WHOLE
                            # observed candle against raw replay after exactly
                            # one venue multiplier. Rebuild only as PARTIAL;
                            # raw presence is still not completeness evidence.
                            if all(row.get(k) is None or pd.isna(row.get(k)) for k in
                                   ("source", "content_sha256", "data_quality", "coverage_ok")):
                                candidate = replay_candidate(tf, opened)
                                if candidate is not None and candidate.close_time == pd.Timestamp(row["close_time"]):
                                    scaled = dict(row)
                                    multiplier = Decimal(str(BASELINE[symbol]["contract_value"]))
                                    for k in ("volume", "quote_volume"):
                                        scaled[k] = Decimal(str(row[k])) * multiplier
                                    scaled_hash = bar_content_sha256(_decision_hash_row(scaled),
                                        open_time=opened, close_time=candidate.close_time, source="canonical_tick_lake")
                                    if scaled_hash == _hash(candidate):
                                        records[idx] = CandleParquetStore._frame([candidate], source="repaired",
                                            data_quality="partial", coverage_ok=False).iloc[0].to_dict()
                                        changed = True
                                        detail["legacy_units_rebuilt_partial"] += 1
                            continue
                        if pd.Timestamp(row["close_time"]) > now:
                            continue
                        closed = row.get("is_closed")
                        if closed is None or pd.isna(closed):
                            candidate = replay_candidate(tf, opened)
                            if candidate is None or _hash(candidate) != row["content_sha256"]:
                                continue
                            row["is_closed"] = True
                            changed = True
                            detail["closed_flags_restored"] += 1
                        if row.get("is_closed") is True:
                            verified[tf].append(store._record_from_row(row, symbol=symbol, timeframe=tf).candle)
                    if changed and apply:
                        # Exact pre-change bytes remain recoverable; no prices,
                        # hashes, quality or coverage fields are rewritten.
                        backup = data_root / "repairs/delta/backups" / (hashlib.sha256(before).hexdigest()+".parquet")
                        if not backup.exists():
                            _atomic(backup, before)
                        _atomic(path, pd.DataFrame(records).to_parquet(index=False))
            if tf != "1m":
                source = LADDER[LADDER.index(tf)-1]
                complete = aggregate_candle_series(symbol, source, tf, sorted(verified[source], key=lambda c:c.open_time))
                missing = [c for c in complete if c.open_time not in present and c.close_time <= now]
                if apply and missing:
                    # Refuse files whose legacy metadata upsert would silently
                    # upgrade unrelated rows. A separate attested replay is needed.
                    safe = []
                    for c in missing:
                        p = store.partition_path(c)
                        if p.exists():
                            old = pd.read_parquet(p)
                            if any(k not in old or old[k].isna().any() for k in
                                   ("is_closed", "source", "content_sha256", "data_quality", "coverage_ok")):
                                continue
                        safe.append(c)
                    store.upsert(safe)
                    missing = safe
                verified[tf].extend(missing)
                detail["parents_inserted"] += len(missing)
            times = sorted(c.open_time for c in verified[tf])
            from vnedge.data.candles import TF_SECONDS
            holes = sum(max(0, int((b-a).total_seconds()/TF_SECONDS[tf])-1)
                        for a,b in zip(times, times[1:]))
            detail["levels"][tf] = {"verified_bars": len(times), "stored_bars": len(present),
                                     "missing_internal_slots": holes,
                                     "first": times[0].isoformat() if times else None,
                                     "last": times[-1].isoformat() if times else None}
            detail["unresolved"][tf] = len(present) - (len(times) - (len(missing) if tf != "1m" else 0))
        detail["arena_required_1h"] = 2160
        detail["arena_history_ready"] = (detail["levels"]["1h"]["verified_bars"] >= 2160
                                          and detail["levels"]["1h"]["missing_internal_slots"] == 0)
        report["symbols"][symbol] = detail
    return report


def repair_delta_lake(data_root: Path, candle_root: Path, *, symbols: tuple[str, ...],
                      apply: bool = False, max_replay_days: int = 2,
                      now: datetime | None = None, environ: Any = None) -> dict[str, Any]:
    if not 0 <= max_replay_days <= 7:
        raise ValueError("delta_repair_day_budget_invalid")
    kwargs = dict(symbols=symbols, apply=apply, max_replay_days=max_replay_days, now=now)
    if not apply:
        return _repair_delta_lake(data_root, candle_root, **kwargs)
    with canonical_write_authority(data_root, "delta_india", environ=os.environ if environ is None else environ):
        return _repair_delta_lake(data_root, candle_root, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--candle-root", type=Path, default=Path("data/candles"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = repair_delta_lake(args.data_root, args.candle_root,
                              symbols=("BTCUSD", "ETHUSD"), apply=args.apply)
    _atomic(args.data_root / "reports/delta_lake_repair.json", json.dumps(report, sort_keys=True).encode())
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
