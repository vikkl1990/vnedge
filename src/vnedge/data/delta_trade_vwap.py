"""Closed Delta India VWAP artifacts derived only from public trade prints.

Delta public trades report integer contract counts.  They are not base-coin
amounts: one BTCUSD contract is 0.001 BTC and one ETHUSD contract is 0.01 ETH.
This module is the single conversion boundary from contracts to base and USD
notional.  Official OHLC candles, HLC3 proxies, BBO rows, and other venues are
deliberately ineligible inputs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pandas as pd

from vnedge.data.symbols import canonical_symbol
from vnedge.exchange.delta_contracts import DeltaContractSpec

DELTA_EXCHANGE = "delta_india"
TRADE_LAKE_SOURCE = "trade_lake"
VwapTimeframe = Literal["1d", "1w"]


def _utc(value: datetime | pd.Timestamp, *, label: str) -> pd.Timestamp:
    result = pd.Timestamp(value)
    result = result.tz_localize("UTC") if result.tzinfo is None else result.tz_convert("UTC")
    if pd.isna(result):
        raise ValueError(f"{label} must be a valid timestamp")
    return result


def _native_symbol(symbol: str) -> str:
    normalized = canonical_symbol(symbol)
    if normalized not in {"BTCUSD", "ETHUSD"}:
        raise ValueError(f"unsupported Delta VWAP symbol: {symbol}")
    return normalized


def _bucket_open(timestamp: pd.Timestamp, timeframe: VwapTimeframe) -> pd.Timestamp:
    day = timestamp.floor("D")
    if timeframe == "1d":
        return day
    return day - pd.Timedelta(days=int(day.dayofweek))


def _bucket_delta(timeframe: VwapTimeframe) -> pd.Timedelta:
    return pd.Timedelta(days=1 if timeframe == "1d" else 7)


@dataclass(frozen=True, slots=True)
class TradeVwapArtifact:
    exchange: str
    symbol: str
    timeframe: VwapTimeframe
    open_time: datetime
    close_time: datetime
    vwap: Decimal
    sum_base: Decimal
    sum_notional: Decimal
    n_trades: int
    source: str = TRADE_LAKE_SOURCE
    coverage_ok: bool = True

    def __post_init__(self) -> None:
        if self.exchange != DELTA_EXCHANGE:
            raise ValueError("trade VWAP artifacts are Delta India only")
        object.__setattr__(self, "symbol", _native_symbol(self.symbol))
        open_time = _utc(self.open_time, label="VWAP open_time")
        close_time = _utc(self.close_time, label="VWAP close_time")
        if close_time <= open_time:
            raise ValueError("VWAP close_time must follow open_time")
        if self.source != TRADE_LAKE_SOURCE or not self.coverage_ok:
            raise ValueError("persisted VWAP artifacts require complete trade-lake coverage")
        if self.sum_base <= 0 or self.sum_notional <= 0 or self.vwap <= 0:
            raise ValueError("VWAP sums and value must be positive")
        if self.n_trades <= 0:
            raise ValueError("VWAP artifact must contain at least one trade")
        if self.vwap != self.sum_notional / self.sum_base:
            raise ValueError("VWAP must equal sum_notional / sum_base")
        object.__setattr__(self, "open_time", open_time.to_pydatetime())
        object.__setattr__(self, "close_time", close_time.to_pydatetime())

    def storage_row(self) -> dict[str, object]:
        row = asdict(self)
        row["vwap"] = str(self.vwap)
        row["sum_base"] = str(self.sum_base)
        row["sum_notional"] = str(self.sum_notional)
        return row


def build_delta_trade_vwap_buckets(
    trades: pd.DataFrame,
    *,
    spec: DeltaContractSpec,
    timeframe: VwapTimeframe,
    closed_through: datetime | pd.Timestamp,
    complete_bucket_opens: Iterable[datetime | pd.Timestamp],
    size_column: str = "size_contracts",
) -> list[TradeVwapArtifact]:
    """Build explicitly coverage-proven daily or weekly artifacts.

    ``complete_bucket_opens`` is supplied by the recorder/gap audit.  A bucket
    is never inferred complete merely because some prints were observed.
    Existing recorder shards use ``amount`` for Delta contract counts; callers
    must opt into that legacy name with ``size_column="amount"`` so the unit
    conversion remains visible at the call site.
    """
    symbol = _native_symbol(spec.symbol)
    if spec.contract_value <= 0:
        raise ValueError("Delta contract_value must be positive")
    required = {"ts_ms", "price", size_column}
    missing = required.difference(trades.columns)
    if missing:
        raise ValueError(f"Delta trade frame missing columns: {sorted(missing)}")
    cutoff = _utc(closed_through, label="closed_through")
    complete = {
        _bucket_open(_utc(value, label="complete bucket"), timeframe)
        for value in complete_bucket_opens
    }
    if trades.empty or not complete:
        return []

    work = trades.loc[:, ["ts_ms", "price", size_column]].copy()
    work["timestamp"] = pd.to_datetime(work["ts_ms"], unit="ms", utc=True, errors="coerce")
    work["price"] = pd.to_numeric(work["price"], errors="coerce")
    work["contracts"] = pd.to_numeric(work[size_column], errors="coerce")
    work = work[
        work["timestamp"].notna()
        & work["price"].gt(0)
        & work["contracts"].gt(0)
        & (work["contracts"] % 1).eq(0)
    ].copy()
    if work.empty:
        return []
    work["bucket_open"] = work["timestamp"].map(lambda ts: _bucket_open(ts, timeframe))
    contract_value = Decimal(str(spec.contract_value))
    delta = _bucket_delta(timeframe)
    artifacts: list[TradeVwapArtifact] = []
    for bucket_open, group in work.groupby("bucket_open", sort=True):
        bucket_open = pd.Timestamp(bucket_open)
        if bucket_open not in complete or bucket_open + delta > cutoff:
            continue
        sum_base = Decimal(0)
        sum_notional = Decimal(0)
        for row in group.itertuples(index=False):
            base = Decimal(str(row.contracts)) * contract_value
            notional = Decimal(str(row.price)) * base
            sum_base += base
            sum_notional += notional
        if sum_base <= 0:
            continue
        artifacts.append(
            TradeVwapArtifact(
                exchange=DELTA_EXCHANGE,
                symbol=symbol,
                timeframe=timeframe,
                open_time=bucket_open.to_pydatetime(),
                close_time=(bucket_open + delta).to_pydatetime(),
                vwap=sum_notional / sum_base,
                sum_base=sum_base,
                sum_notional=sum_notional,
                n_trades=len(group),
            )
        )
    return artifacts


def roll_weekly_vwap_from_daily(
    daily: Iterable[TradeVwapArtifact],
    *,
    closed_through: datetime | pd.Timestamp,
) -> list[TradeVwapArtifact]:
    """Roll seven complete daily accumulators into closed UTC weeks."""
    rows = sorted(daily, key=lambda item: item.open_time)
    if any(item.timeframe != "1d" for item in rows):
        raise ValueError("weekly VWAP rollup accepts daily artifacts only")
    cutoff = _utc(closed_through, label="closed_through")
    grouped: dict[pd.Timestamp, list[TradeVwapArtifact]] = {}
    for item in rows:
        week_open = _bucket_open(pd.Timestamp(item.open_time), "1w")
        grouped.setdefault(week_open, []).append(item)
    out: list[TradeVwapArtifact] = []
    for week_open, group in sorted(grouped.items()):
        expected = [week_open + pd.Timedelta(days=index) for index in range(7)]
        actual = [pd.Timestamp(item.open_time) for item in group]
        if actual != expected or week_open + pd.Timedelta(days=7) > cutoff:
            continue
        sum_base = sum((item.sum_base for item in group), Decimal(0))
        sum_notional = sum((item.sum_notional for item in group), Decimal(0))
        if sum_base <= 0:
            continue
        out.append(
            TradeVwapArtifact(
                exchange=DELTA_EXCHANGE,
                symbol=group[0].symbol,
                timeframe="1w",
                open_time=week_open.to_pydatetime(),
                close_time=(week_open + pd.Timedelta(days=7)).to_pydatetime(),
                vwap=sum_notional / sum_base,
                sum_base=sum_base,
                sum_notional=sum_notional,
                n_trades=sum(item.n_trades for item in group),
            )
        )
    return out


class TradeVwapArtifactStore:
    """Atomic, identity-keyed store for derived VWAP artifacts."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path(self, symbol: str, timeframe: VwapTimeframe) -> Path:
        return (
            self.root
            / "derived"
            / "vwap"
            / f"exchange={DELTA_EXCHANGE}"
            / f"symbol={_native_symbol(symbol)}"
            / f"timeframe={timeframe}"
            / "vwap.parquet"
        )

    def upsert(self, artifacts: Iterable[TradeVwapArtifact]) -> Path | None:
        rows = list(artifacts)
        if not rows:
            return None
        identities = {(item.symbol, item.timeframe) for item in rows}
        if len(identities) != 1:
            raise ValueError("one VWAP upsert may contain only one symbol/timeframe")
        path = self.path(rows[0].symbol, rows[0].timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        incoming = pd.DataFrame([item.storage_row() for item in rows])
        if path.exists():
            incoming = pd.concat([pd.read_parquet(path), incoming], ignore_index=True)
        incoming = (
            incoming.drop_duplicates(["exchange", "symbol", "timeframe", "open_time"], keep="last")
            .sort_values("open_time")
            .reset_index(drop=True)
        )
        tmp = path.with_suffix(".parquet.tmp")
        incoming.to_parquet(tmp, index=False)
        tmp.replace(path)
        return path

    def read(self, symbol: str, timeframe: VwapTimeframe) -> pd.DataFrame:
        path = self.path(symbol, timeframe)
        if not path.exists():
            return pd.DataFrame()
        return pd.read_parquet(path)


__all__ = [
    "DELTA_EXCHANGE",
    "TRADE_LAKE_SOURCE",
    "TradeVwapArtifact",
    "TradeVwapArtifactStore",
    "build_delta_trade_vwap_buckets",
    "roll_weekly_vwap_from_daily",
]
