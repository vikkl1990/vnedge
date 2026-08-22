"""Continuous lake health — detect holes always, claim health only on evidence.

The 2026-08-19 audit found three symbols had each silently lost an hour of
tape while the cockpit reported ``ok``.  A one-shot scanner fixes that day; it
does not keep the lake healthy.  This module runs the check on a cycle and
publishes a status that is *earned* rather than assumed.

Design constraints, learned from that audit:

* **Detection is always safe, so it always runs.**  It reads the candle store
  and compares each bar's ``open_time`` against the previous ``close_time``.
  It needs no external truth and cannot be fooled by an empty gap store.
* **Recovery is a separate strict worker.** Detection never mutates candle
  history. The deployed Binance worker may repair a recorded hole only when
  aggregate-trade IDs prove a contiguous interval; a failed proof leaves the
  gap open and readiness remains false.
* **A clean status must mean "checked and clean", never "never checked".**
  Before the first cycle the state is UNKNOWN, not healthy. An empty/short or
  stale-ended lake is DEGRADED even though it has no interior discontinuity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from vnedge.data.candle_gap_scan import scan
from vnedge.data.candles import TF_SECONDS, CandleParquetStore

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(minutes=15)


class LakeStatus(str, Enum):
    UNKNOWN = "unknown"   # never checked -- must not read as healthy
    HEALTHY = "healthy"   # checked, no holes
    DEGRADED = "degraded"  # holes present
    ERROR = "error"       # the check itself failed


@dataclass
class LakeHealth:
    """Latest evidence about the lake, safe to publish to the dashboard."""

    status: LakeStatus = LakeStatus.UNKNOWN
    checked_at: datetime | None = None
    holes_by_symbol: dict[str, int] = field(default_factory=dict)
    bars_by_symbol: dict[str, int] = field(default_factory=dict)
    latest_close_by_symbol: dict[str, str | None] = field(default_factory=dict)
    detail: str = "no check has run yet"

    @property
    def total_holes(self) -> int:
        return sum(self.holes_by_symbol.values())

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "holes_by_symbol": dict(self.holes_by_symbol),
            "bars_by_symbol": dict(self.bars_by_symbol),
            "latest_close_by_symbol": dict(self.latest_close_by_symbol),
            "total_holes": self.total_holes,
            "detail": self.detail,
        }


@dataclass
class LakeHealthMonitor:
    """Periodic scan of the candle lake; optionally repairs, always verifies."""

    exchange: str
    symbols: Sequence[str]
    candle_root: Path
    gap_root: Path
    timeframe: str = "1h"
    interval: timedelta = DEFAULT_INTERVAL
    auto_recover: bool = False
    minimum_bars: int = 2
    tail_grace: timedelta = timedelta(minutes=10)
    status_path: Path | None = None
    health: LakeHealth = field(default_factory=LakeHealth)

    def __post_init__(self) -> None:
        if self.status_path is None:
            self.status_path = self.gap_root / "lake_health.json"

    def _publish(self) -> None:
        """Atomically expose the latest scan to other processes/the dashboard."""
        if self.status_path is None:
            return
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_suffix(self.status_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.health.as_dict(), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.status_path)
        except OSError:
            logger.exception("failed to publish lake health status")

    def check_once(self, *, now: datetime | None = None) -> LakeHealth:
        """One detection cycle.  Never raises: a failed check reports ERROR."""
        moment = now or datetime.now(UTC)
        try:
            found = scan(
                exchange=self.exchange,
                symbols=list(self.symbols),
                timeframe=self.timeframe,
                candle_root=self.candle_root,
                gap_root=self.gap_root,
                write=True,
                now=moment,
            )
            timeframe_seconds = TF_SECONDS[self.timeframe]
            candle_store = CandleParquetStore(
                self.candle_root,
                exchange=self.exchange,
            )
            bars_by_symbol: dict[str, int] = {}
            latest_close_by_symbol: dict[str, str | None] = {}
            evidence_failures: list[str] = []
            for symbol in self.symbols:
                candles = candle_store.read(symbol, self.timeframe)
                bars_by_symbol[symbol] = len(candles)
                latest_close = max(
                    (candle.close_time for candle in candles),
                    default=None,
                )
                latest_close_by_symbol[symbol] = (
                    latest_close.isoformat() if latest_close is not None else None
                )
                if len(candles) < self.minimum_bars:
                    evidence_failures.append(
                        f"{symbol}=only {len(candles)}/{self.minimum_bars} bars"
                    )
                    continue
                if latest_close is None:
                    evidence_failures.append(f"{symbol}=no closed tail")
                    continue
                tail_age = moment - latest_close
                max_tail_age = timedelta(seconds=timeframe_seconds) + self.tail_grace
                if tail_age < timedelta(0):
                    evidence_failures.append(f"{symbol}=future closed tail")
                elif tail_age > max_tail_age:
                    evidence_failures.append(
                        f"{symbol}=tail stale {tail_age.total_seconds():.0f}s"
                    )
        except Exception as exc:
            logger.exception("lake health scan failed")
            self.health = LakeHealth(
                status=LakeStatus.ERROR,
                checked_at=moment,
                detail=f"scan failed: {exc}",
            )
            self._publish()
            return self.health

        counts = {symbol: len(holes) for symbol, holes in found.items()}
        total = sum(counts.values())
        if total == 0 and not evidence_failures:
            self.health = LakeHealth(
                status=LakeStatus.HEALTHY,
                checked_at=moment,
                holes_by_symbol=counts,
                bars_by_symbol=bars_by_symbol,
                latest_close_by_symbol=latest_close_by_symbol,
                detail=f"{len(counts)} symbol(s) contiguous on {self.timeframe}",
            )
        else:
            worst = ", ".join(
                f"{sym}={n}" for sym, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n
            )
            self.health = LakeHealth(
                status=LakeStatus.DEGRADED,
                checked_at=moment,
                holes_by_symbol=counts,
                bars_by_symbol=bars_by_symbol,
                latest_close_by_symbol=latest_close_by_symbol,
                detail="; ".join(
                    part
                    for part in (
                        f"{total} hole(s) recorded: {worst}" if total else "",
                        ", ".join(evidence_failures),
                    )
                    if part
                ),
            )
            logger.warning("lake health: %s", self.health.detail)
        self._publish()
        return self.health

    async def run(self) -> None:
        """Forever: check, sleep, check.  Survives individual failures."""
        while True:
            self.check_once()
            await asyncio.sleep(self.interval.total_seconds())
