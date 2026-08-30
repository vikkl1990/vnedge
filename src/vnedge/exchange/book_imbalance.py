"""Realtime order-book imbalance derived only from venue book frames.

Transport heartbeats, funding updates, trades, and candles must never call
``BookTape.on_book``.  The tape is deliberately tiny: one immutable snapshot
whose event timestamp is supplied by the venue.  It is an acceptance filter,
not a holding clock, sizing input, or fill-price model.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class BookImbalance:
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    imb: float
    microprice: float
    spread_ticks: float
    ts: datetime
    levels: int

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise ValueError("book event timestamp must be timezone-aware")
        object.__setattr__(self, "ts", self.ts.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class MultiLevelMicro:
    """Distance-weighted depth and microprice from one Delta L2 frame.

    Sizes remain in the venue's native unit (contracts).  This is descriptive
    book context only: it is never a fill price, mark price, sizing input, or
    holding clock.
    """

    bid: float
    ask: float
    mid: float
    microprice: float
    imb: float
    q_bid: float
    q_ask: float
    spread_ticks: float
    levels_used: int
    ts: datetime

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise ValueError("book event timestamp must be timezone-aware")
        object.__setattr__(self, "ts", self.ts.astimezone(UTC))


def _valid_side(
    levels: Sequence[tuple[float, float]], *, descending: bool
) -> list[tuple[float, float]]:
    """Return the valid, correctly ordered prefix of one L2 side."""

    out: list[tuple[float, float]] = []
    last: float | None = None
    for raw_price, raw_size in levels:
        try:
            price = float(raw_price)
            size = float(raw_size)
        except (TypeError, ValueError):
            continue
        if not (
            math.isfinite(price)
            and math.isfinite(size)
            and price > 0
            and size > 0
        ):
            continue
        if last is not None:
            if descending and price >= last:
                break
            if not descending and price <= last:
                break
        out.append((price, size))
        last = price
    return out


def multilevel_microprice(
    bids: Sequence[tuple[float, float]],
    asks: Sequence[tuple[float, float]],
    *,
    tick: float,
    ts: datetime,
    levels: int = 5,
    decay_k: float = 0.35,
) -> MultiLevelMicro | None:
    """Return distance-weighted L2 depth from one venue book frame.

    ``decay_k=0`` is the raw sum of the first ``levels`` valid levels.
    Positive decay applies ``exp(-k * ticks_from_touch)`` so distant size
    cannot dominate the touch without an explicit parameter choice.
    """

    if not math.isfinite(tick) or tick <= 0:
        raise ValueError("tick must be positive")
    if levels <= 0:
        raise ValueError("levels must be positive")
    if not math.isfinite(decay_k) or decay_k < 0:
        raise ValueError("decay_k must be finite and non-negative")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("book event timestamp must be timezone-aware")

    clean_bids = _valid_side(bids, descending=True)[:levels]
    clean_asks = _valid_side(asks, descending=False)[:levels]
    if not clean_bids or not clean_asks:
        return None

    bid = clean_bids[0][0]
    ask = clean_asks[0][0]
    if bid >= ask:
        return None
    spread = ask - bid
    if not math.isfinite(spread) or spread <= 0:
        return None

    def weighted_depth(
        side: Sequence[tuple[float, float]], *, is_bid: bool
    ) -> float:
        total = 0.0
        for price, size in side:
            ticks_from_touch = (bid - price) / tick if is_bid else (price - ask) / tick
            if ticks_from_touch < 0 or not math.isfinite(ticks_from_touch):
                return math.nan
            weight = 1.0 if decay_k == 0 else math.exp(-decay_k * ticks_from_touch)
            total += size * weight
        return total

    q_bid = weighted_depth(clean_bids, is_bid=True)
    q_ask = weighted_depth(clean_asks, is_bid=False)
    total = q_bid + q_ask
    if not (
        math.isfinite(q_bid)
        and math.isfinite(q_ask)
        and math.isfinite(total)
        and q_bid > 0
        and q_ask > 0
        and total > 0
    ):
        return None
    microprice = (ask * q_bid + bid * q_ask) / total
    if not math.isfinite(microprice) or not bid <= microprice <= ask:
        return None
    return MultiLevelMicro(
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2.0,
        microprice=microprice,
        imb=(q_bid - q_ask) / total,
        q_bid=q_bid,
        q_ask=q_ask,
        spread_ticks=spread / tick,
        levels_used=min(len(clean_bids), len(clean_asks)),
        ts=ts,
    )


def imbalance_l1(
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
    *,
    tick: float,
    ts: datetime,
) -> BookImbalance | None:
    """Return one valid top-of-book snapshot, or ``None`` fail-closed."""

    values = (bid, ask, bid_size, ask_size, tick)
    if not all(math.isfinite(value) and value > 0 for value in values):
        return None
    # A locked book has no executable spread and is not valid evidence for
    # quote acceptance. Treat it exactly like a crossed book and fail closed.
    if ask <= bid:
        return None
    total = bid_size + ask_size
    if not math.isfinite(total) or total <= 0:
        return None
    return BookImbalance(
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        imb=(bid_size - ask_size) / total,
        # More bid depth shifts the microprice toward the ask and vice versa.
        microprice=(ask * bid_size + bid * ask_size) / total,
        spread_ticks=(ask - bid) / tick,
        ts=ts,
        levels=1,
    )


def imbalance_l2(
    bids: Sequence[tuple[float, float]],
    asks: Sequence[tuple[float, float]],
    *,
    tick: float,
    ts: datetime,
    levels: int = 5,
    decay_k: float = 0.35,
) -> BookImbalance | None:
    """Adapt distance-weighted L2 context to the scanner's book contract."""

    micro = multilevel_microprice(
        bids,
        asks,
        tick=tick,
        ts=ts,
        levels=levels,
        decay_k=decay_k,
    )
    if micro is None:
        return None
    return BookImbalance(
        bid=micro.bid,
        ask=micro.ask,
        bid_size=micro.q_bid,
        ask_size=micro.q_ask,
        imb=micro.imb,
        microprice=micro.microprice,
        spread_ticks=micro.spread_ticks,
        ts=micro.ts,
        levels=micro.levels_used,
    )


@dataclass(slots=True)
class BookTape:
    stale_after_s: float = 2.0
    last: BookImbalance | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.stale_after_s) or self.stale_after_s <= 0:
            raise ValueError("book stale threshold must be positive")

    def on_book(self, snapshot: BookImbalance) -> None:
        self.last = snapshot

    def live(self, now: datetime) -> BookImbalance | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("book freshness clock must be timezone-aware")
        if self.last is None:
            return None
        age = (now.astimezone(UTC) - self.last.ts).total_seconds()
        if age < 0 or age > self.stale_after_s:
            return None
        return self.last


def imbalance_allows(
    side: str,
    book: BookImbalance | None,
    *,
    min_abs: float = 0.20,
    max_spread_ticks: float = 2.0,
) -> str | None:
    """Return a stable rejection reason or ``None`` when the probe may fire."""

    if side not in {"long", "short"}:
        raise ValueError(f"unsupported side: {side}")
    if not 0 <= min_abs <= 1:
        raise ValueError("min_abs must be within [0, 1]")
    if max_spread_ticks <= 0:
        raise ValueError("max_spread_ticks must be positive")
    if book is None:
        return "book_stale"
    if book.spread_ticks > max_spread_ticks:
        return "spread_too_wide"
    if side == "long" and book.imb < min_abs:
        return "imb_not_bid_heavy"
    if side == "short" and book.imb > -min_abs:
        return "imb_not_ask_heavy"
    return None


__all__ = [
    "BookImbalance",
    "BookTape",
    "MultiLevelMicro",
    "imbalance_allows",
    "imbalance_l1",
    "imbalance_l2",
    "multilevel_microprice",
]
