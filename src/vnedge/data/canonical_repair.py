"""Staged, conflict-detecting merge for canonical candle repair.

Repair workers may fetch and rebuild freely in a run-scoped staging lake.  The
canonical lake is changed only by this owner-authorized merge after a complete
preflight proves that no staged identity disagrees with an existing candle.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from vnedge.data.candles import TF_SECONDS, Candle, CandleParquetStore, CandlePipeline
from vnedge.data.symbols import canonical_symbol
from vnedge.exchange.writer_lease import canonical_write_authority


class CanonicalRepairConflictError(RuntimeError):
    """A staged candle disagrees with an immutable canonical identity."""


@dataclass(frozen=True, slots=True)
class CanonicalRepairMerge:
    exchange: str
    staged_rows: int
    inserted_rows: int
    identical_rows: int
    merged_at: str


def _append_report(root: Path, report: CanonicalRepairMerge) -> None:
    path = root / "merge-reports.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(report), sort_keys=True, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def merge_staged_candles(
    *,
    staging_root: Path | str,
    canonical_root: Path | str,
    authority_root: Path | str,
    exchange: str,
    symbols: list[str],
) -> CanonicalRepairMerge:
    """Preflight every staged row, then merge missing identities under lease."""
    stage_path = Path(staging_root)
    staged = CandleParquetStore(stage_path, exchange=exchange)
    canonical = CandleParquetStore(canonical_root, exchange=exchange)
    missing: list[Candle] = []
    identical = 0
    touched: set[str] = set()

    # Preflight happens before acquiring the short mutation lease and before
    # any write.  The merge rechecks while holding the lease to close TOCTOU.
    proposed: list[Candle] = []
    for raw_symbol in symbols:
        symbol = canonical_symbol(raw_symbol)
        for timeframe in TF_SECONDS:
            rows = staged.read(symbol, timeframe)
            proposed.extend(rows)
            if rows:
                touched.add(symbol)

    with canonical_write_authority(Path(authority_root), exchange):
        for candle in proposed:
            current = canonical.read_at(candle.symbol, candle.timeframe, candle.open_time)
            if current is None:
                missing.append(candle)
            elif current == candle:
                identical += 1
            else:
                raise CanonicalRepairConflictError(
                    "staged repair conflicts with canonical candle "
                    f"{exchange}/{candle.symbol}/{candle.timeframe}/"
                    f"{candle.open_time.isoformat()}"
                )
        if missing:
            canonical.upsert(missing)
        for symbol in sorted(touched):
            CandlePipeline(symbol, store=canonical).reconcile_aggregates()

    report = CanonicalRepairMerge(
        exchange=exchange,
        staged_rows=len(proposed),
        inserted_rows=len(missing),
        identical_rows=identical,
        merged_at=datetime.now(UTC).isoformat(),
    )
    _append_report(stage_path, report)
    return report
