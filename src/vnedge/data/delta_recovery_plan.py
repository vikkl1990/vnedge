"""Bounded, read-only recovery planning. Raw-day inventory is NOT coverage.

Only existing hash-verified closed children can prove a reconstructable parent.
This module neither writes candles nor authorizes a raw replay. Plans are stale
as soon as an input partition changes; the canonical owner must revalidate.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vnedge.data.candles import Candle, CandleParquetStore, TF_SECONDS, _decision_hash_row, aggregate_candle_series, floor_time
from vnedge.data.symbols import canonical_symbol
from vnedge.strategy.arm_evidence import assert_decision_row

LADDER = ("1m", "5m", "15m", "1h")
MAX_ROWS = 250_000
MAX_RANGES = 256
MAX_CANDIDATES = 128


def verified_candle(row: dict[str, Any], symbol: str, tf: str, now: datetime) -> Candle:
    for key in ("open_time", "close_time", "content_sha256", "source", "data_quality"):
        if row.get(key) is None or pd.isna(row[key]):
            raise ValueError(f"persisted_{key}_missing")
    for key in ("is_closed", "coverage_ok"):
        if not isinstance(row.get(key), (bool, np.bool_)) or not row[key]:
            raise ValueError(f"{key}_unproven")
    if row["source"] != "canonical_tick_lake":
        raise ValueError("source_not_canonical_tick_lake")
    for key, expected in (("exchange", "delta_india"), ("symbol", symbol), ("timeframe", tf)):
        if key in row and row[key] != expected:
            raise ValueError(f"partition_{key}_mismatch")
    values = _decision_hash_row(row)
    values.update(timestamp=row["open_time"], candle_source=row["source"])
    ref = assert_decision_row(values, timeframe=tf)
    if ref.close_time > now:
        raise ValueError("future_bar")
    return CandleParquetStore(".", exchange="delta_india")._record_from_row(row, symbol=symbol, timeframe=tf).candle


def _hash(candle: Candle) -> str:
    return str(CandleParquetStore._frame([candle], source="canonical_tick_lake", data_quality="ok", coverage_ok=True).iloc[0].content_sha256)


def _read_level(directory: Path, symbol: str, tf: str, start: datetime, end: datetime,
                inputs: list[dict[str, Any]]) -> tuple[dict[datetime, list[dict[str, Any]]], set[str]]:
    grouped: dict[datetime, list[dict[str, Any]]] = {}
    unsafe_partitions: set[str] = set()
    scanned = 0
    for path in sorted((directory / tf).glob("*.parquet")):
        # Canonical file dates bound I/O; unknown names are inspected, not ignored.
        try:
            file_start = datetime.strptime(path.stem, "%Y-%m" if tf == "1h" else "%Y-%m-%d").replace(tzinfo=UTC)
            file_end = ((file_start.replace(day=28) + timedelta(days=4)).replace(day=1)
                        if tf == "1h" else file_start + timedelta(days=1))
            if file_end <= start or file_start >= end:
                continue
        except ValueError:
            pass
        if path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError("partition_byte_budget_exceeded")
        data = path.read_bytes()
        import pyarrow.parquet as pq
        count = pq.ParquetFile(io.BytesIO(data)).metadata.num_rows
        scanned += count
        if scanned > MAX_ROWS:
            raise ValueError("partition_row_budget_exceeded")
        frame = pd.read_parquet(io.BytesIO(data))
        inputs.append({"path": f"{symbol}/{tf}/{path.name}", "sha256": hashlib.sha256(data).hexdigest(), "rows": count})
        # Existing upsert can fill absent legacy metadata. Never propose it
        # for a parent partition containing such rows, including outside window.
        if any(k not in frame or frame[k].isna().any() for k in
               ("is_closed", "source", "content_sha256", "data_quality", "coverage_ok")):
            unsafe_partitions.add(path.name)
        for row in frame.to_dict("records"):
            opened = pd.Timestamp(row.get("open_time"))
            if pd.isna(opened) or opened.tzinfo is None:
                raise ValueError("unlocatable_partition_row")
            opened = opened.to_pydatetime()
            if start <= opened < end:
                grouped.setdefault(opened, []).append(row)
    return grouped, unsafe_partitions


def build_delta_recovery_plan(data_root: Path, candle_root: Path, *, symbol: str,
                              required_hours: int = 2160, as_of: datetime | None = None) -> dict[str, Any]:
    symbol = canonical_symbol(symbol)
    if symbol not in {"BTCUSD", "ETHUSD"} or not 1 <= required_hours <= 2160:
        raise ValueError("unsupported_recovery_plan_scope")
    now = as_of or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("recovery_plan_requires_aware_cutoff")
    end = floor_time(now.astimezone(UTC), "1h")
    start = end - timedelta(hours=required_hours)
    directory = candle_root / "exchange=delta_india" / symbol
    inputs: list[dict[str, Any]] = []
    invalid: Counter[str] = Counter()
    errors: list[str] = []
    present: dict[str, dict[datetime, list[dict[str, Any]]]] = {}
    actual: dict[str, dict[datetime, Candle]] = {}
    possible: dict[str, dict[datetime, Candle]] = {}
    hashes: dict[str, dict[datetime, str]] = {}
    candidates: list[dict[str, Any]] = []
    unsafe: dict[str, set[str]] = {}
    blocked: dict[str, set[datetime]] = {}
    store = CandleParquetStore(candle_root, exchange="delta_india")
    for tf in LADDER:
        try:
            rows, unsafe[tf] = _read_level(directory, symbol, tf, start, end, inputs)
        except Exception as exc:
            errors.append(f"{tf}:{type(exc).__name__}:{exc}")
            rows, unsafe[tf] = {}, set()
        present[tf] = rows
        actual[tf] = {}
        hashes[tf] = {}
        for opened, copies in rows.items():
            if len(copies) != 1:
                invalid[f"{tf}:duplicate_slot"] += 1
                continue
            try:
                actual[tf][opened] = verified_candle(copies[0], symbol, tf, now)
                hashes[tf][opened] = str(copies[0]["content_sha256"])
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                invalid[f"{tf}:{exc}"] += 1
        possible[tf] = dict(actual[tf])
        blocked[tf] = set()
        if tf != "1m":
            source = LADDER[LADDER.index(tf)-1]
            derived = aggregate_candle_series(symbol, source, tf, sorted(possible[source].values(), key=lambda c: c.open_time))
            if not errors:
                for candle in derived:
                    if candle.open_time in rows:
                        continue  # never overwrite even an invalid published row
                    if store.partition_path(candle).name in unsafe[tf]:
                        blocked[tf].add(candle.open_time)
                        continue
                    possible[tf][candle.open_time] = candle
                    hashes[tf][candle.open_time] = _hash(candle)
                    children = [possible[source][candle.open_time + timedelta(seconds=i)]
                                for i in range(0, TF_SECONDS[tf], TF_SECONDS[source])]
                    candidates.append({"timeframe": tf, "open_time": candle.open_time.isoformat(),
                        "content_sha256": hashes[tf][candle.open_time], "child_timeframe": source,
                        "child_hashes": [hashes[source][c.open_time] for c in children],
                        "requires_parent_chain": any(c.open_time not in actual[source] for c in children)})
    # Raw directory names are an inventory hint only, not proof of an interval.
    tape = data_root / "ticks/exchange=delta_india" / f"symbol={symbol}" / "stream=trades"
    raw_days: set[str] = set()
    try:
        for day in sorted(tape.glob("*")):
            try:
                day_open = datetime.strptime(day.name, "%Y%m%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if day_open >= end or day_open + timedelta(days=1) <= start:
                continue
            if day.is_dir() and any(day.glob("*.parquet")):
                raw_days.add(day.name)
    except OSError as exc:
        errors.append(f"raw_inventory:{type(exc).__name__}")
    counts: Counter[str] = Counter()
    ranges: list[dict[str, Any]] = []
    for i in range(required_hours):
        opened = start + timedelta(hours=i)
        if errors:
            status = "INPUT_UNREADABLE"
        elif opened in actual["1h"]:
            status = "VERIFIED"
        elif opened in present["1h"]:
            status = "PRESENT_PROOF_INVALID"
        elif opened in possible["1h"]:
            status = "REBUILD_FROM_VERIFIED_CHILDREN"
        elif opened in blocked["1h"]:
            status = "TARGET_PARTITION_UNSAFE"
        elif opened.strftime("%Y%m%d") in raw_days:
            status = "RAW_DAY_PRESENT_COVERAGE_UNPROVEN"
        else:
            status = "NO_LOCAL_RAW_DAY"
        counts[status] += 1
        if status == "VERIFIED":
            continue
        if ranges and ranges[-1]["status"] == status and ranges[-1]["close_time"] == opened.isoformat():
            ranges[-1]["close_time"] = (opened + timedelta(hours=1)).isoformat()
            ranges[-1]["hours"] += 1
        else:
            ranges.append({"open_time": opened.isoformat(), "close_time": (opened+timedelta(hours=1)).isoformat(),
                           "hours": 1, "status": status})
    plan = {"schema_version": 1, "generated_at": now.isoformat(), "exchange": "delta_india", "symbol": symbol,
        "scope": "rolling_hourly_inventory_not_experiment_admission", "window_open": start.isoformat(), "window_close": end.isoformat(),
        "required_hours": required_hours, "status": "ERROR" if errors else "GAPS_REMAIN" if ranges else "VERIFIED_WINDOW",
        "counts": dict(sorted(counts.items())), "invalid_row_counts": dict(sorted(invalid.items())),
        "ranges": ranges[-MAX_RANGES:], "range_count": len(ranges), "ranges_truncated": len(ranges) > MAX_RANGES,
        "rebuild_candidates": candidates[:MAX_CANDIDATES] if not errors else [],
        "rebuild_candidate_count": len(candidates) if not errors else 0,
        "rebuild_candidates_truncated": not errors and len(candidates) > MAX_CANDIDATES,
        "unsafe_target_partitions": {tf: sorted(names) for tf, names in unsafe.items() if names},
        "blocked_parent_counts": {tf: len(slots) for tf, slots in blocked.items() if slots},
        "partition_inputs": inputs, "raw_days": sorted(raw_days), "errors": errors,
        "raw_completeness": "UNPROVEN_NO_SUPPORTED_MANIFEST", "writer": "canonical_owner_only",
        "can_apply": False, "can_trade": False, "can_promote": False}
    plan["plan_id"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return plan


def build_delta_recovery_report(data_root: Path, candle_root: Path, *,
                                symbols: tuple[str, ...], as_of: datetime | None = None) -> dict[str, Any]:
    """Projection for the owner's maintenance cycle; never affects repair/admission."""
    now = as_of or datetime.now(UTC)
    reports: dict[str, Any] = {}
    for symbol in symbols:
        try:
            reports[symbol] = build_delta_recovery_plan(data_root, candle_root, symbol=symbol, as_of=now)
        except Exception as exc:
            reports[symbol] = {"status": "ERROR", "errors": [f"{type(exc).__name__}:{exc}"],
                               "can_apply": False, "can_trade": False, "can_promote": False}
    return {"schema_version": 1, "generated_at": now.isoformat(), "symbols": reports,
            "can_apply": False, "can_trade": False, "can_promote": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--candle-root", type=Path, default=Path("data/candles"))
    parser.add_argument("--symbol", choices=("BTCUSD", "ETHUSD"), required=True)
    parser.add_argument("--required-hours", type=int, default=2160)
    args = parser.parse_args()
    print(json.dumps(build_delta_recovery_plan(args.data_root, args.candle_root,
        symbol=args.symbol, required_hours=args.required_hours), sort_keys=True))


if __name__ == "__main__":
    main()
