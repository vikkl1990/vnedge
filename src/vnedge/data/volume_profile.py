"""Deterministic trade-derived volume profile measurements.

MEASUREMENT/RESEARCH ONLY: profiles describe volume-at-price inside an
explicit closed window. They cannot emit signals, intents, orders, capital
permission, or promotion decisions. Missing trade coverage returns no profile;
bar-volume proxies and synthetic prices are deliberately unsupported.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import pandas as pd

from vnedge.data.symbols import canonical_symbol

_ZERO = Decimal(0)
DEFAULT_VALUE_AREA_FRACTION = Decimal("0.70")
DEFAULT_BIN_SIZE_BY_SYMBOL: Mapping[str, Decimal] = {
    "BTCUSDT": Decimal(10),
    "ETHUSDT": Decimal(1),
    "SOLUSDT": Decimal("0.1"),
}


def _utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_parameters(bin_size: Decimal, value_area_fraction: Decimal) -> None:
    if not bin_size.is_finite() or bin_size <= 0:
        raise ValueError("volume-profile bin_size must be finite and positive")
    if (
        not value_area_fraction.is_finite()
        or value_area_fraction <= 0
        or value_area_fraction > 1
    ):
        raise ValueError("value_area_fraction must be in (0, 1]")


def _partition_value(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not normalized or any(
        not (character.isalnum() or character in {"_", "-", "."})
        for character in normalized
    ):
        raise ValueError(f"invalid {label} partition value")
    return normalized


def profile_bin_size(symbol: str) -> Decimal:
    """Return the frozen fixed-price bin for a supported Pulse symbol."""
    canonical = canonical_symbol(symbol)
    try:
        return DEFAULT_BIN_SIZE_BY_SYMBOL[canonical]
    except KeyError as exc:
        raise KeyError(f"no frozen volume-profile bin size for {canonical}") from exc


@dataclass(frozen=True, slots=True)
class VolumeProfile:
    window_start: datetime
    window_end: datetime
    bin_size: Decimal
    poc: Decimal
    value_area_low: Decimal
    value_area_high: Decimal
    total_volume: Decimal
    trade_count: int
    realized_value_area_fraction: Decimal
    value_area_fraction: Decimal = DEFAULT_VALUE_AREA_FRACTION
    source: str = "trades"

    def __post_init__(self) -> None:
        start = _utc(self.window_start, label="profile window_start")
        end = _utc(self.window_end, label="profile window_end")
        if end <= start:
            raise ValueError("profile window_end must be after window_start")
        _validate_parameters(self.bin_size, self.value_area_fraction)
        if self.trade_count < 1 or self.total_volume <= 0:
            raise ValueError("volume profile requires positive trades and volume")
        if (
            self.realized_value_area_fraction < self.value_area_fraction
            or self.realized_value_area_fraction > 1
        ):
            raise ValueError("realized value-area fraction must be between target and 1")
        if not self.value_area_low <= self.poc <= self.value_area_high:
            raise ValueError("POC must lie inside the value area")
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)

    def to_dict(self) -> dict[str, object]:
        return {
            "available": True,
            "source": self.source,
            "start": self.window_start.isoformat().replace("+00:00", "Z"),
            "end": self.window_end.isoformat().replace("+00:00", "Z"),
            "bin_size": float(self.bin_size),
            "poc": float(self.poc),
            "val": float(self.value_area_low),
            "vah": float(self.value_area_high),
            "value_area_low": float(self.value_area_low),
            "value_area_high": float(self.value_area_high),
            "value_area_fraction": float(self.value_area_fraction),
            "target_pct": float(self.value_area_fraction),
            "va_volume_pct": float(self.realized_value_area_fraction),
            "total_volume": float(self.total_volume),
            "trade_count": self.trade_count,
        }


def _profile_from_buckets(
    buckets: Mapping[int, Decimal],
    *,
    window_start: datetime,
    window_end: datetime,
    bin_size: Decimal,
    trade_count: int,
    value_area_fraction: Decimal,
) -> VolumeProfile:
    if not buckets:
        raise ValueError("volume profile requires at least one valid trade")
    total = sum(buckets.values(), _ZERO)
    if total <= 0:
        raise ValueError("volume profile requires positive total volume")
    # Stable tie-break: the lower price bin wins. This makes replay hashes
    # independent of dict insertion or parquet shard ordering.
    poc_index = min(buckets, key=lambda index: (-buckets[index], index))
    low_index = high_index = poc_index
    included = buckets[poc_index]
    target = total * value_area_fraction
    minimum = min(buckets)
    maximum = max(buckets)
    while included < target and (low_index > minimum or high_index < maximum):
        below = buckets.get(low_index - 1, _ZERO) if low_index > minimum else None
        above = buckets.get(high_index + 1, _ZERO) if high_index < maximum else None
        # Frozen tie rule: expand upward when both adjacent bins have equal
        # volume. Direction never depends on source row or shard ordering.
        if above is not None and (below is None or above >= below):
            high_index += 1
            included += above
        elif below is not None:
            low_index -= 1
            included += below
    return VolumeProfile(
        window_start=window_start,
        window_end=window_end,
        bin_size=bin_size,
        poc=(Decimal(poc_index) + Decimal("0.5")) * bin_size,
        value_area_low=Decimal(low_index) * bin_size,
        value_area_high=Decimal(high_index + 1) * bin_size,
        total_volume=total,
        trade_count=trade_count,
        value_area_fraction=value_area_fraction,
        realized_value_area_fraction=included / total,
    )


def volume_profile(
    prices: Sequence[Decimal],
    volumes: Sequence[Decimal],
    bin_size: Decimal,
    *,
    window_start: datetime,
    window_end: datetime,
    value_area_fraction: Decimal = DEFAULT_VALUE_AREA_FRACTION,
) -> VolumeProfile:
    """Build an exact volume-by-price profile from ordered trade observations."""
    _validate_parameters(bin_size, value_area_fraction)
    if len(prices) != len(volumes):
        raise ValueError("prices and volumes must have equal length")
    buckets: dict[int, Decimal] = {}
    valid = 0
    for price, amount in zip(prices, volumes, strict=True):
        if not price.is_finite() or price <= 0:
            continue
        if not amount.is_finite() or amount <= 0:
            continue
        index = int((price / bin_size).to_integral_value(rounding=ROUND_FLOOR))
        buckets[index] = buckets.get(index, _ZERO) + amount
        valid += 1
    return _profile_from_buckets(
        buckets,
        window_start=window_start,
        window_end=window_end,
        bin_size=bin_size,
        trade_count=valid,
        value_area_fraction=value_area_fraction,
    )


def point_of_control(
    prices: Sequence[Decimal],
    volumes: Sequence[Decimal],
    bin_size: Decimal,
) -> Decimal:
    """Return the midpoint of the highest-volume price bin."""
    profile = volume_profile(
        prices,
        volumes,
        bin_size,
        window_start=datetime(1970, 1, 1, tzinfo=UTC),
        window_end=datetime(1970, 1, 2, tzinfo=UTC),
    )
    return profile.poc


def profile_location(
    price: Decimal | float | None,
    profile: VolumeProfile | None,
) -> str:
    """Classify a reference price against the closed profile value area."""
    if price is None or profile is None:
        return "unavailable"
    value = price if isinstance(price, Decimal) else Decimal(str(price))
    if not value.is_finite() or value <= 0:
        return "unavailable"
    if value == profile.value_area_low or value == profile.value_area_high:
        return "at_value_edge"
    if value > profile.value_area_high:
        return "above_value"
    if value < profile.value_area_low:
        return "below_value"
    return "inside_value"


class VolumeProfileArtifactStore:
    """Persist deterministic closed-window measurements as atomic JSON."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @staticmethod
    def window_id(
        exchange: str,
        symbol: str,
        window_kind: str,
        window_start: datetime,
    ) -> str:
        canonical = canonical_symbol(symbol)
        start = _utc(window_start, label="artifact window_start")
        safe_exchange = _partition_value(exchange, label="exchange")
        safe_symbol = _partition_value(canonical, label="symbol")
        safe_window = _partition_value(window_kind, label="window")
        return (
            f"{safe_exchange}:{safe_symbol}:{safe_window}:"
            f"{start.date().isoformat()}"
        )

    def path_for(
        self,
        exchange: str,
        symbol: str,
        window_kind: str,
        window_start: datetime,
    ) -> Path:
        canonical = canonical_symbol(symbol)
        start = _utc(window_start, label="artifact window_start")
        safe_exchange = _partition_value(exchange, label="exchange")
        safe_symbol = _partition_value(canonical, label="symbol")
        safe_window = _partition_value(window_kind, label="window")
        return (
            self.root
            / f"exchange={safe_exchange}"
            / f"symbol={safe_symbol}"
            / f"window={safe_window}"
            / f"{start.date().isoformat()}.json"
        )

    def put(
        self,
        *,
        exchange: str,
        symbol: str,
        window_kind: str,
        source_exchange: str,
        profile: VolumeProfile,
    ) -> tuple[str, Path]:
        window_id = self.window_id(
            exchange, symbol, window_kind, profile.window_start
        )
        path = self.path_for(exchange, symbol, window_kind, profile.window_start)
        payload = {
            "schema_version": "1.0",
            "window_id": window_id,
            "exchange": exchange,
            "symbol": canonical_symbol(symbol),
            "window": window_kind,
            "source_exchange": source_exchange,
            "profile": profile.to_dict(),
            "measurement_only": True,
            "can_trade": False,
            "can_promote": False,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == encoded:
            return window_id, path
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return window_id, path


class TickLakeVolumeProfileStore:
    """Read immutable trade shards and cache deterministic closed profiles."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self._cache: dict[
            tuple[str, str, datetime, datetime, Decimal],
            tuple[tuple[tuple[str, int], ...], VolumeProfile | None],
        ] = {}

    def _day_directories(
        self,
        exchange: str,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[Path]:
        safe_exchange = _partition_value(exchange, label="exchange")
        canonical = canonical_symbol(symbol)
        safe_symbol = _partition_value(canonical, label="symbol")
        base = (
            self.data_root
            / "ticks"
            / f"exchange={safe_exchange}"
            / f"symbol={safe_symbol}"
            / "stream=trades"
        )
        day = start.date()
        final_day = (end - timedelta(microseconds=1)).date()
        directories: list[Path] = []
        while day <= final_day:
            for label in (day.strftime("%Y%m%d"), day.isoformat()):
                candidate = base / label
                if candidate.is_dir():
                    directories.append(candidate)
            day += timedelta(days=1)
        return directories

    @staticmethod
    def _signature(directories: Sequence[Path]) -> tuple[tuple[str, int], ...]:
        return tuple(
            (str(directory), directory.stat().st_mtime_ns)
            for directory in directories
        )

    def read(
        self,
        exchange: str,
        symbol: str,
        start: datetime,
        end: datetime,
        bin_size: Decimal,
    ) -> VolumeProfile | None:
        start = _utc(start, label="profile start")
        end = _utc(end, label="profile end")
        if end <= start:
            raise ValueError("profile end must be after start")
        _validate_parameters(bin_size, DEFAULT_VALUE_AREA_FRACTION)
        directories = self._day_directories(exchange, symbol, start, end)
        signature = self._signature(directories)
        key = (exchange, symbol, start, end, bin_size)
        cached = self._cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        paths = [
            path
            for directory in directories
            for path in sorted(directory.glob("*.parquet"))
        ]
        if not paths:
            self._cache[key] = (signature, None)
            return None
        import pyarrow.dataset as ds

        table = ds.dataset([str(path) for path in paths], format="parquet").to_table(
            columns=["ts_ms", "price", "amount"]
        )
        frame = table.to_pandas()
        result = self._from_frame(frame, start=start, end=end, bin_size=bin_size)
        self._cache[key] = (signature, result)
        return result

    @staticmethod
    def _from_frame(
        frame: pd.DataFrame,
        *,
        start: datetime,
        end: datetime,
        bin_size: Decimal,
    ) -> VolumeProfile | None:
        missing = {"ts_ms", "price", "amount"} - set(frame.columns)
        if missing:
            raise ValueError(f"trade frame missing required columns: {sorted(missing)}")
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        timestamps = pd.to_numeric(frame["ts_ms"], errors="coerce")
        prices = pd.to_numeric(frame["price"], errors="coerce")
        amounts = pd.to_numeric(frame["amount"], errors="coerce")
        valid = (
            timestamps.ge(start_ms)
            & timestamps.lt(end_ms)
            & prices.gt(0)
            & amounts.gt(0)
            & prices.map(math.isfinite)
            & amounts.map(math.isfinite)
        )
        if not valid.any():
            return None
        selected_prices = prices[valid]
        selected_amounts = amounts[valid]
        indexes = (selected_prices / float(bin_size)).map(math.floor)
        grouped = selected_amounts.groupby(indexes).sum().sort_index()
        buckets = {
            int(index): Decimal(str(amount))
            for index, amount in grouped.items()
            if math.isfinite(float(amount)) and float(amount) > 0
        }
        if not buckets:
            return None
        return _profile_from_buckets(
            buckets,
            window_start=start,
            window_end=end,
            bin_size=bin_size,
            trade_count=int(valid.sum()),
            value_area_fraction=DEFAULT_VALUE_AREA_FRACTION,
        )
