"""Decimal VWAP primitives for bars, sessions, and research windows.

VWAP is always derived from traded quote volume divided by traded base volume.
OHLC midpoints and averages of child-bar VWAP values are deliberately absent
from this module because neither is a volume-weighted calculation.

MEASUREMENT/RESEARCH ONLY: AnchoredVWAP and dual_avwap_bias cannot set
``tradeable=True``, grant capital eligibility, emit an OrderIntent, or bypass
the registry, CostGate, risk gateway, kill switch, or mode ladder.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from vnedge.data.candles import Candle

ZERO = Decimal(0)
BPS = Decimal(10_000)
DecimalLike = Decimal | int | float | str


def _decimal(value: DecimalLike) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _valid_positive(value: Decimal) -> bool:
    return value.is_finite() and value > ZERO


def _utc(timestamp: datetime, *, label: str) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return timestamp.astimezone(UTC)


def vwap_from_sums(
    quote_volume: DecimalLike,
    base_volume: DecimalLike,
) -> Decimal | None:
    """Return ``Σ(price × amount) / Σ(amount)`` or ``None`` for invalid sums."""
    quote = _decimal(quote_volume)
    base = _decimal(base_volume)
    if not _valid_positive(quote) or not _valid_positive(base):
        return None
    return quote / base


def vwap_from_trades(
    trades: Iterable[tuple[DecimalLike, DecimalLike]],
) -> Decimal | None:
    """Calculate VWAP from ``(price, base_amount)`` trades.

    Invalid/non-positive ticks are skipped instead of contaminating the window.
    """
    quote_volume = ZERO
    base_volume = ZERO
    for raw_price, raw_amount in trades:
        price = _decimal(raw_price)
        amount = _decimal(raw_amount)
        if not _valid_positive(price) or not _valid_positive(amount):
            continue
        quote_volume += price * amount
        base_volume += amount
    return vwap_from_sums(quote_volume, base_volume)


def vwap_merge(
    quote_volumes: Sequence[DecimalLike],
    base_volumes: Sequence[DecimalLike],
) -> Decimal | None:
    """Merge child windows by sums, never by averaging their VWAP values."""
    if len(quote_volumes) != len(base_volumes):
        raise ValueError("quote_volumes and base_volumes must have equal lengths")
    quote_volume = ZERO
    base_volume = ZERO
    for raw_quote, raw_base in zip(quote_volumes, base_volumes, strict=True):
        quote = _decimal(raw_quote)
        base = _decimal(raw_base)
        if not quote.is_finite() or not base.is_finite() or quote < ZERO or base < ZERO:
            return None
        if base == ZERO:
            if quote != ZERO:
                return None
            continue
        if quote == ZERO:
            return None
        quote_volume += quote
        base_volume += base
    return vwap_from_sums(quote_volume, base_volume)


def price_vs_vwap_bps(price: DecimalLike, vwap: DecimalLike | None) -> Decimal | None:
    """Signed distance from VWAP; positive means price is above VWAP."""
    if vwap is None:
        return None
    price_value = _decimal(price)
    vwap_value = _decimal(vwap)
    if not _valid_positive(price_value) or not _valid_positive(vwap_value):
        return None
    return (price_value - vwap_value) * BPS / vwap_value


def quantize_to_tick(price: DecimalLike, tick_size: DecimalLike) -> Decimal | None:
    """Round a positive price to the nearest valid tick using half-up ties."""
    price_value = _decimal(price)
    tick = _decimal(tick_size)
    if not _valid_positive(price_value) or not _valid_positive(tick):
        return None
    ticks = (price_value / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return (ticks * tick).quantize(tick)


@dataclass(slots=True)
class RunningVWAP:
    """Mutable accumulator for one explicitly controlled VWAP window."""

    quote_volume: Decimal = ZERO
    base_volume: Decimal = ZERO
    trade_count: int = 0

    def update(self, price: DecimalLike, amount: DecimalLike) -> Decimal | None:
        price_value = _decimal(price)
        amount_value = _decimal(amount)
        if not _valid_positive(price_value) or not _valid_positive(amount_value):
            return self.value
        self.quote_volume += price_value * amount_value
        self.base_volume += amount_value
        self.trade_count += 1
        return self.value

    def update_sums(
        self,
        quote_volume: DecimalLike,
        base_volume: DecimalLike,
        *,
        trade_count: int = 0,
    ) -> Decimal | None:
        """Add an exact child-window contribution without averaging its VWAP."""
        if trade_count < 0:
            raise ValueError("trade_count must be non-negative")
        quote = _decimal(quote_volume)
        base = _decimal(base_volume)
        if quote == ZERO and base == ZERO:
            return self.value
        if not _valid_positive(quote) or not _valid_positive(base):
            return self.value
        self.quote_volume += quote
        self.base_volume += base
        self.trade_count += trade_count
        return self.value

    @property
    def value(self) -> Decimal | None:
        return vwap_from_sums(self.quote_volume, self.base_volume)

    def reset(self) -> None:
        self.quote_volume = ZERO
        self.base_volume = ZERO
        self.trade_count = 0


@dataclass(slots=True)
class CandleVWAPAccumulator:
    """Running trade VWAP scoped to one caller-managed candle window."""

    symbol: str
    timeframe: str
    open_time: datetime
    _running: RunningVWAP = field(default_factory=RunningVWAP, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.timeframe.strip():
            raise ValueError("symbol and timeframe must not be empty")
        if self.open_time.tzinfo is None or self.open_time.utcoffset() is None:
            raise ValueError("candle VWAP open_time must be timezone-aware")
        self.open_time = self.open_time.astimezone(UTC)

    def on_trade(self, price: DecimalLike, amount: DecimalLike) -> Decimal | None:
        return self._running.update(price, amount)

    @property
    def vwap(self) -> Decimal | None:
        return self._running.value

    @property
    def volume(self) -> Decimal:
        return self._running.base_volume

    @property
    def quote_volume(self) -> Decimal:
        return self._running.quote_volume

    @property
    def trade_count(self) -> int:
        return self._running.trade_count


@dataclass(slots=True)
class SessionVWAP:
    """VWAP anchored at a configurable UTC hour and reset on each boundary."""

    session_start_hour_utc: int = 0
    _day_key: str | None = field(default=None, init=False, repr=False)
    _running: RunningVWAP = field(default_factory=RunningVWAP, init=False, repr=False)
    _last_timestamp: datetime | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not 0 <= self.session_start_hour_utc <= 23:
            raise ValueError("session_start_hour_utc must be in [0, 23]")

    def _session_key(self, timestamp: datetime) -> str:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("session VWAP timestamps must be timezone-aware")
        utc_timestamp = timestamp.astimezone(UTC)
        if utc_timestamp.hour < self.session_start_hour_utc:
            utc_timestamp -= timedelta(days=1)
        return utc_timestamp.date().isoformat()

    def on_trade(
        self,
        timestamp: datetime,
        price: DecimalLike,
        amount: DecimalLike,
    ) -> Decimal | None:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("session VWAP timestamps must be timezone-aware")
        timestamp = timestamp.astimezone(UTC)
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ValueError("session VWAP trades must be ordered by timestamp")
        key = self._session_key(timestamp)
        if self._day_key != key:
            self._day_key = key
            self._running.reset()
        self._last_timestamp = timestamp
        return self._running.update(price, amount)

    @property
    def vwap(self) -> Decimal | None:
        return self._running.value

    @property
    def volume(self) -> Decimal:
        return self._running.base_volume

    @property
    def quote_volume(self) -> Decimal:
        return self._running.quote_volume

    @property
    def trade_count(self) -> int:
        return self._running.trade_count


@dataclass(slots=True)
class AnchoredVWAP:
    """VWAP accumulated from one explicit event timestamp onward.

    One instance consumes either exact trades or closed candles, never both, to
    prevent accidental double counting. A timestamp inside a candle does not
    include that partial candle; bar mode begins with the first bar whose
    ``open_time`` is at or after the anchor. Bar mode always uses the candle's
    exact ``quote_volume / volume`` contribution, never HLC3 or close proxies.
    """

    anchor_time: datetime
    anchor_label: str | None = None
    _running: RunningVWAP = field(default_factory=RunningVWAP, init=False, repr=False)
    _input_mode: Literal["trade", "candle"] | None = field(default=None, init=False, repr=False)
    _last_event_time: datetime | None = field(default=None, init=False, repr=False)
    _symbol: str | None = field(default=None, init=False, repr=False)
    _timeframe: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.anchor_time = _utc(self.anchor_time, label="AVWAP anchor_time")
        if self.anchor_label is not None:
            self.anchor_label = self.anchor_label.strip() or None

    @classmethod
    def from_bar(cls, candle: Candle, *, label: str | None = None) -> AnchoredVWAP:
        if not candle.is_closed:
            raise ValueError("AVWAP anchor bar must be closed")
        anchored = cls(candle.open_time, anchor_label=label)
        anchored._symbol = candle.symbol
        anchored._timeframe = candle.timeframe
        return anchored

    def _select_mode(self, mode: Literal["trade", "candle"]) -> None:
        if self._input_mode is not None and self._input_mode != mode:
            raise ValueError("AnchoredVWAP cannot mix trade and candle inputs")
        self._input_mode = mode

    def on_trade(
        self,
        timestamp: datetime,
        price: DecimalLike,
        amount: DecimalLike,
    ) -> Decimal | None:
        timestamp = _utc(timestamp, label="AVWAP trade timestamp")
        if self._last_event_time is not None and timestamp < self._last_event_time:
            raise ValueError("AVWAP trades must be ordered by timestamp")
        if timestamp < self.anchor_time:
            return self.value
        self._select_mode("trade")
        self._last_event_time = timestamp
        return self._running.update(price, amount)

    def on_candle(self, candle: Candle) -> Decimal | None:
        if not candle.is_closed:
            raise ValueError("AVWAP accepts closed candles only")
        if self._last_event_time is not None and candle.open_time < self._last_event_time:
            raise ValueError("AVWAP candles must be ordered and non-overlapping")
        if candle.open_time < self.anchor_time:
            return self.value
        if self._symbol is None:
            self._symbol = candle.symbol
            self._timeframe = candle.timeframe
        elif candle.symbol != self._symbol or candle.timeframe != self._timeframe:
            raise ValueError("AVWAP candle inputs must share symbol and timeframe")
        self._select_mode("candle")
        self._last_event_time = candle.close_time
        return self._running.update_sums(
            candle.quote_volume,
            candle.volume,
            trade_count=candle.trade_count,
        )

    def reanchor(self, anchor_time: datetime, *, label: str | None = None) -> None:
        self.anchor_time = _utc(anchor_time, label="AVWAP anchor_time")
        self.anchor_label = (label.strip() or None) if label is not None else None
        self._running.reset()
        self._input_mode = None
        self._last_event_time = None
        self._symbol = None
        self._timeframe = None

    @property
    def value(self) -> Decimal | None:
        return self._running.value

    @property
    def volume(self) -> Decimal:
        return self._running.base_volume

    @property
    def quote_volume(self) -> Decimal:
        return self._running.quote_volume

    @property
    def trade_count(self) -> int:
        return self._running.trade_count


@dataclass(frozen=True, slots=True)
class SwingAnchor:
    """Mechanical swing anchor plus the first time it is knowable."""

    kind: Literal["swing_low", "swing_high"]
    bar_index: int
    anchor_time: datetime
    confirmed_at: datetime
    price: Decimal
    length: int

    def is_confirmed(self, at: datetime) -> bool:
        """Return whether this mechanical anchor is knowable at ``at``."""
        return _utc(at, label="swing confirmation time") >= self.confirmed_at


def anchored_vwap_series(
    candles: Sequence[Candle],
    anchor: int | datetime,
) -> tuple[Decimal | None, ...]:
    """Return AVWAP at every bar from an index or explicit timestamp anchor."""
    if isinstance(anchor, int):
        if not 0 <= anchor < len(candles):
            raise IndexError("AVWAP anchor index is outside the candle series")
        running = AnchoredVWAP.from_bar(candles[anchor])
    else:
        running = AnchoredVWAP(anchor)
    return tuple(running.on_candle(candle) for candle in candles)


def confirmed_swing_anchors(
    candles: Sequence[Candle],
    *,
    length: int = 3,
) -> tuple[SwingAnchor, ...]:
    """Find unique L-left/L-right swing extrema in a closed candle series.

    A swing at index ``i`` is unavailable to research logic until bar ``i+L``
    closes. ``confirmed_at`` carries that boundary so callers cannot silently
    act on the anchor with future knowledge.
    """
    if length < 1:
        raise ValueError("swing length must be >= 1")
    previous_open: datetime | None = None
    symbol: str | None = None
    timeframe: str | None = None
    for candle in candles:
        if not candle.is_closed:
            raise ValueError("swing anchors require closed candles")
        if symbol is None:
            symbol, timeframe = candle.symbol, candle.timeframe
        elif candle.symbol != symbol or candle.timeframe != timeframe:
            raise ValueError("swing candle series must share symbol and timeframe")
        if previous_open is not None and candle.open_time <= previous_open:
            raise ValueError("swing candle series must be strictly ordered")
        previous_open = candle.open_time

    anchors = []
    for index in range(length, len(candles) - length):
        window = candles[index - length : index + length + 1]
        candidate = candles[index]
        lows = [candle.low for candle in window]
        highs = [candle.high for candle in window]
        confirmed_at = candles[index + length].close_time
        if candidate.low == min(lows) and lows.count(candidate.low) == 1:
            anchors.append(
                SwingAnchor(
                    "swing_low",
                    index,
                    candidate.open_time,
                    confirmed_at,
                    candidate.low,
                    length,
                )
            )
        if candidate.high == max(highs) and highs.count(candidate.high) == 1:
            anchors.append(
                SwingAnchor(
                    "swing_high",
                    index,
                    candidate.open_time,
                    confirmed_at,
                    candidate.high,
                    length,
                )
            )
    return tuple(anchors)


DualAVWAPBias = Literal["strong_long", "strong_short", "between", "unavailable"]


def dual_avwap_bias(
    price: DecimalLike,
    swing_low_avwap: DecimalLike | None,
    swing_high_avwap: DecimalLike | None,
) -> DualAVWAPBias:
    """Classify price relative to AVWAPs from significant low/high anchors."""
    if swing_low_avwap is None or swing_high_avwap is None:
        return "unavailable"
    price_value = _decimal(price)
    low_value = _decimal(swing_low_avwap)
    high_value = _decimal(swing_high_avwap)
    if not all(_valid_positive(value) for value in (price_value, low_value, high_value)):
        return "unavailable"
    if price_value > max(low_value, high_value):
        return "strong_long"
    if price_value < min(low_value, high_value):
        return "strong_short"
    return "between"
