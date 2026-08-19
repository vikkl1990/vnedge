"""Continuous lake health — detect holes always, claim health only on evidence.

The 2026-08-19 audit found three symbols had each silently lost an hour of
tape while the cockpit reported ``ok``.  A one-shot scanner fixes that day; it
does not keep the lake healthy.  This module runs the check on a cycle and
publishes a status that is *earned* rather than assumed.

Design constraints, learned from that audit:

* **Detection is always safe, so it always runs.**  It reads the candle store
  and compares each bar's ``open_time`` against the previous ``close_time``.
  It needs no external truth and cannot be fooled by an empty gap store.
* **Recovery is not automatic by default.**  ``binance_gap_recovery`` marks a
  gap ``recovered`` on thin evidence and the tick lake carries no trade ids to
  dedupe a refetched boundary, so unattended backfill can corrupt volume and
  VWAP.  Recovery is opt-in and always followed by a re-scan.
* **A clean status must mean "checked and clean", never "never checked".**
  Before the first cycle the state is UNKNOWN, not healthy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from vnedge.data.candle_gap_scan import scan

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
    detail: str = "no check has run yet"

    @property
    def total_holes(self) -> int:
        return sum(self.holes_by_symbol.values())

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "holes_by_symbol": dict(self.holes_by_symbol),
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
    health: LakeHealth = field(default_factory=LakeHealth)

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
        except Exception as exc:  # noqa: BLE001 - status must degrade, not crash
            logger.exception("lake health scan failed")
            self.health = LakeHealth(
                status=LakeStatus.ERROR,
                checked_at=moment,
                detail=f"scan failed: {exc}",
            )
            return self.health

        counts = {symbol: len(holes) for symbol, holes in found.items()}
        total = sum(counts.values())
        if total == 0:
            self.health = LakeHealth(
                status=LakeStatus.HEALTHY,
                checked_at=moment,
                holes_by_symbol=counts,
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
                detail=f"{total} hole(s) recorded: {worst}",
            )
            logger.warning("lake health: %s", self.health.detail)
        return self.health

    async def run(self) -> None:
        """Forever: check, sleep, check.  Survives individual failures."""
        while True:
            self.check_once()
            await asyncio.sleep(self.interval.total_seconds())
