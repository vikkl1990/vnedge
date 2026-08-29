"""Safe reads over the raw tick lake.

The recorder validates prints at ingestion (``_normalize_trade_batch``), but
that guard only landed on 2026-08-16 (2d99425). Every shard written before it
can carry ``price == 0`` placeholders from the public websocket cache:
measured at 164,925 rows across 35 days of BTCUSDT, roughly 0.09%.

They are harmless to a sum and destructive to anything else. A zero price
sinks any ``min``/``low`` to zero, and in a fill test it reads as "price
traded down to your limit" and manufactures a fill on every long. Both
failures were observed: a footprint study returned identical medians for
every group, and an L2 fill replay over-reported touches by 17 percentage
points (79% against a true 62%).

Callers that read ``stream=trades`` directly should come through here rather
than reimplement the guard, because the failure is silent in both directions.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The ingestion guard landed here; shards written earlier are unvalidated.
INGEST_GUARD_LANDED = "2026-08-16"


@dataclass(frozen=True, slots=True)
class TapeCleanResult:
    """What a sanity pass removed, so callers can report it rather than hide it."""

    rows_in: int
    rows_out: int

    @property
    def dropped(self) -> int:
        return self.rows_in - self.rows_out

    @property
    def dropped_fraction(self) -> float:
        return self.dropped / self.rows_in if self.rows_in else 0.0


def clean_trades(frame):
    """Drop unusable prints from a raw trades frame.

    Returns ``(frame, TapeCleanResult)``. The count is returned rather than
    logged so an analysis can state how much it discarded -- a silent filter
    is how a corrupt window becomes an invisible one.
    """
    rows_in = len(frame)
    if rows_in and {"price", "amount"} <= set(frame.columns):
        frame = frame[(frame["price"] > 0) & (frame["amount"] > 0)]
    return frame, TapeCleanResult(rows_in=rows_in, rows_out=len(frame))


def clean_book(frame):
    """Drop unusable book snapshots (non-positive or crossed top of book)."""
    rows_in = len(frame)
    if rows_in and {"bid", "ask"} <= set(frame.columns):
        frame = frame[
            (frame["bid"] > 0)
            & (frame["ask"] > 0)
            & (frame["ask"] >= frame["bid"])
        ]
    return frame, TapeCleanResult(rows_in=rows_in, rows_out=len(frame))
