"""Bounded raw-print inspection. Timestamp density is never completeness proof."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from vnedge.data.symbols import canonical_symbol
from vnedge.exchange.delta_snapshot_validation import BASELINE

MAX_ROWS = 2_000_000
MAX_SHARDS = 10_000
MAX_BYTES = 256 * 1024 * 1024
logger = logging.getLogger(__name__)


def shard_bytes(root: Path, relative: str, symbol: str) -> bytes:
    path = root / relative
    base = root / "ticks/exchange=delta_india" / f"symbol={symbol}" / "stream=trades"
    if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("shard_outside_symbol_tape")
    if path.suffix != ".parquet" or path.stat().st_size > MAX_BYTES:
        raise ValueError("shard_byte_budget_or_type")
    return path.read_bytes()


def load_raw_shards(
    root: Path,
    symbol: str,
    paths: list[str],
    *,
    start: datetime,
    end: datetime,
    expected_hashes: dict[str, str] | None = None,
) -> tuple[list[tuple[int, Decimal, Decimal, str]], dict[str, Any]]:
    """Return ordered, undeduplicated prints plus audit; raises on unreadable input."""
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("invalid_audit_interval")
    if symbol not in BASELINE or len(paths) > MAX_SHARDS or len(set(paths)) != len(paths):
        raise ValueError("invalid_shard_scope")
    multiplier = Decimal(str(BASELINE[symbol]["contract_value"]))
    lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    rows: list[tuple[int, Decimal, Decimal, str]] = []
    counts: Counter[str] = Counter()
    seen_ids: dict[str, tuple[Any, ...]] = {}
    idless: set[tuple[Any, ...]] = set()
    shards = []
    scanned = total_bytes = 0
    previous: int | None = None
    observed_timestamps: list[int] = []
    for relative in paths:
        data = shard_bytes(root, relative, symbol)
        total_bytes += len(data)
        digest = hashlib.sha256(data).hexdigest()
        if total_bytes > MAX_BYTES:
            raise ValueError("audit_byte_budget_exceeded")
        if expected_hashes is not None and expected_hashes.get(relative) != digest:
            raise ValueError("shard_hash_mismatch")
        count = pq.ParquetFile(io.BytesIO(data)).metadata.num_rows
        scanned += count
        if scanned > MAX_ROWS:
            raise ValueError("audit_row_budget_exceeded")
        shards.append({"path": relative, "sha256": digest, "rows": count})
        frame = pd.read_parquet(io.BytesIO(data))
        for row in frame.to_dict("records"):
            try:
                raw_ts = Decimal(str(row.get("ts_ms")))
                if not raw_ts.is_finite() or raw_ts != raw_ts.to_integral_value():
                    raise ValueError("timestamp_invalid")
                ts = int(raw_ts)
                if not lo <= ts < hi:
                    counts["outside_interval"] += 1
                    continue
                if previous is not None and ts < previous:
                    counts["out_of_order_transitions"] += 1
                previous = ts
                counts["interval_rows"] += 1
                observed_timestamps.append(ts)
                if row.get("exchange_timestamped") is not True:
                    counts["exchange_time_unproven"] += 1
                # Legacy amount alone has no persisted units proof. Do not infer
                # contracts merely because scaling happens to match a candle.
                if any(
                    k not in row or pd.isna(row[k])
                    for k in ("size_contracts", "contract_value", "base_amount", "amount")
                ):
                    counts["legacy_units_unproven"] += 1
                    continue
                price = Decimal(str(row["price"]))
                contracts = Decimal(str(row["size_contracts"]))
                if (
                    not price.is_finite()
                    or price <= 0
                    or not contracts.is_finite()
                    or contracts <= 0
                    or contracts != contracts.to_integral_value()
                ):
                    raise ValueError("invalid_price_or_contracts")
                if (
                    Decimal(str(row["contract_value"])) != multiplier
                    or Decimal(str(row["amount"])) != contracts
                    or Decimal(str(row["base_amount"])) != contracts * multiplier
                ):
                    raise ValueError("contract_conversion_mismatch")
                side = str(row.get("side", ""))
                if side not in {"buy", "sell"}:
                    raise ValueError("side_unproven")
                body = (ts, price, contracts, side)
                trade_id = row.get("trade_id")
                if trade_id is None or pd.isna(trade_id) or str(trade_id) == "":
                    counts["without_trade_id"] += 1
                    if body in idless:
                        counts["identical_idless_rows"] += 1
                    idless.add(body)
                else:
                    key = str(trade_id)
                    if key in seen_ids:
                        counts["duplicate_ids" if seen_ids[key] == body else "conflicting_ids"] += 1
                    seen_ids[key] = body
                rows.append((ts, price, contracts * multiplier, side))
            except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError) as exc:
                counts["invalid_rows"] += 1
                counts[f"invalid:{type(exc).__name__}:{exc}"] += 1
    rows.sort(key=lambda r: r[0])
    timestamps = sorted(observed_timestamps)
    report = {
        "schema_version": 1,
        "exchange": "delta_india",
        "symbol": symbol,
        "window_open": start.isoformat(),
        "window_close": end.isoformat(),
        "shards": shards,
        "scanned_rows": scanned,
        "counts": dict(counts),
        "valid_rows": len(rows),
        "first_ts_ms": timestamps[0] if timestamps else None,
        "last_ts_ms": timestamps[-1] if timestamps else None,
        "max_interprint_gap_ms": max((b - a for a, b in pairwise(timestamps)), default=None),
        "contract_value": str(multiplier),
        "coverage": "UNPROVEN",
        "can_replay_as_canonical": False,
        "can_trade": False,
    }
    return rows, report


def audit_raw_day(root: Path, symbol: str, day: str) -> dict[str, Any]:
    symbol = canonical_symbol(symbol)
    start = datetime.strptime(day, "%Y%m%d").replace(tzinfo=UTC)
    directory = root / "ticks/exchange=delta_india" / f"symbol={symbol}" / "stream=trades" / day
    paths = [str(p.relative_to(root)) for p in sorted(directory.glob("*.parquet"))]
    try:
        _, report = load_raw_shards(root, symbol, paths, start=start, end=start + timedelta(days=1))
        report["status"] = "AUDITED_UNPROVEN" if paths else "NO_LOCAL_SHARDS"
        return report
    except Exception as exc:
        logger.exception("Delta raw audit failed; coverage remains unproven")
        return {
            "symbol": symbol,
            "day": day,
            "status": "ERROR",
            "reason": f"{type(exc).__name__}:{exc}",
            "coverage": "UNPROVEN",
            "can_replay_as_canonical": False,
            "can_trade": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--symbol", choices=("BTCUSD", "ETHUSD"), required=True)
    parser.add_argument("--day", required=True, help="UTC YYYYMMDD, one bounded day")
    args = parser.parse_args()
    print(json.dumps(audit_raw_day(args.data_root, args.symbol, args.day), sort_keys=True))


if __name__ == "__main__":
    main()
