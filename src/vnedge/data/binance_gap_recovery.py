"""Recover Binance USDM storage holes from the public aggregate-trade API.

This command is intentionally narrower than the research archive backfill:

* it reads only unrecovered ``storage_hole`` records;
* it fetches the exact half-open interval ``[start, end)`` from Binance;
* aggregate-trade IDs must be strictly contiguous across every REST page;
* shards are atomically added to the live tick-lake partition;
* canonical candles are replayed before a gap may be marked recovered.

Exchange OHLCV is never used as proof of trade/VWAP continuity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, Self

import httpx
import pandas as pd

from vnedge.data.aggtrades_backfill import TRADE_SCHEMA, shard_dir
from vnedge.data.candle_bootstrap import BootstrapReport, bootstrap_candles
from vnedge.data.candles import CandlePipeline
from vnedge.data.gaps import GapKind, GapParquetStore

logger = logging.getLogger(__name__)

BASE_URL = "https://fapi.binance.com/fapi/v1/aggTrades"
MAX_WINDOW = timedelta(hours=1)
DEFAULT_PAGE_SIZE = 1_000
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.55


def _utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _market_id(symbol: str) -> str:
    return symbol.split(":", 1)[0].replace("/", "").upper()


class IntervalFetcher(Protocol):
    def fetch(self, symbol: str, start: datetime, end: datetime) -> FetchedTape: ...


@dataclass(frozen=True, slots=True)
class FetchedTape:
    symbol: str
    start: datetime
    end: datetime
    frame: pd.DataFrame
    first_agg_id: int
    last_agg_id: int
    requests: int
    sha256: str

    @property
    def trades(self) -> int:
        return len(self.frame)


@dataclass(frozen=True, slots=True)
class RecoveredGap:
    symbol: str
    gap_id: str
    start: str
    end: str
    trades: int
    first_agg_id: int
    last_agg_id: int
    requests: int
    sha256: str
    shards: tuple[str, ...]
    candles: int
    unrelated_replay_rejections: int


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    exchange: str
    recovered: tuple[RecoveredGap, ...]
    skipped_symbols: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "recovered": [asdict(item) for item in self.recovered],
            "skipped_symbols": list(self.skipped_symbols),
            "trades": sum(item.trades for item in self.recovered),
            "candles": sum(item.candles for item in self.recovered),
        }


class BinanceAggTradeRest:
    """Rate-limited public client with strict aggregate-ID continuity checks."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
        max_retries: int = 5,
    ) -> None:
        if not 1 <= page_size <= DEFAULT_PAGE_SIZE:
            raise ValueError("page_size must be within [1, 1000]")
        if request_interval_seconds < 0 or max_retries < 1:
            raise ValueError("request interval/retry settings are invalid")
        self.client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None
        self.page_size = page_size
        self.request_interval_seconds = request_interval_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, params: dict[str, int | str]) -> list[dict[str, Any]]:
        wait = self.request_interval_seconds - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(1, self.max_retries + 1):
            response = self.client.get(BASE_URL, params=params)
            self._last_request_at = time.monotonic()
            if response.status_code not in {418, 429}:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError("Binance aggTrades response is not a list")
                return [row for row in payload if isinstance(row, dict)]
            if attempt == self.max_retries:
                response.raise_for_status()
            retry_after = float(response.headers.get("Retry-After", "1"))
            time.sleep(max(retry_after, self.request_interval_seconds * 2))
        raise RuntimeError("unreachable aggregate-trade retry state")

    @staticmethod
    def _row(raw: dict[str, Any]) -> tuple[int, int, float, float, str]:
        agg_id = int(raw["a"])
        timestamp = int(raw["T"])
        price = float(Decimal(str(raw["p"])))
        amount = float(Decimal(str(raw["q"])))
        side = "sell" if bool(raw["m"]) else "buy"
        if agg_id < 0 or timestamp <= 0 or price <= 0 or amount <= 0:
            raise ValueError("Binance aggTrade contains invalid values")
        return agg_id, timestamp, price, amount, side

    def fetch(self, symbol: str, start: datetime, end: datetime) -> FetchedTape:
        start = _utc(start, label="gap start")
        end = _utc(end, label="gap end")
        if end <= start:
            raise ValueError("gap end must be after start")
        market = _market_id(symbol)
        start_ms = int(start.timestamp() * 1_000)
        end_ms = int(end.timestamp() * 1_000)
        cursor = start_ms
        previous_id: int | None = None
        raw_rows: list[tuple[int, int, float, float, str]] = []
        requests = 0

        while cursor < end_ms:
            window_end = min(cursor + int(MAX_WINDOW.total_seconds() * 1_000), end_ms)
            from_id: int | None = None
            while True:
                params: dict[str, int | str] = {
                    "symbol": market,
                    "startTime": cursor,
                    "endTime": window_end - 1,
                    "limit": self.page_size,
                }
                if from_id is not None:
                    params["fromId"] = from_id
                page = self._request(params)
                requests += 1
                if not page:
                    break
                converted = [self._row(row) for row in page]
                for row in converted:
                    agg_id, timestamp, _price, _amount, _side = row
                    if not cursor <= timestamp < window_end:
                        raise ValueError(
                            f"Binance returned timestamp {timestamp} outside requested window"
                        )
                    if previous_id is not None and agg_id != previous_id + 1:
                        raise ValueError(
                            f"aggregate trade ID discontinuity: expected "
                            f"{previous_id + 1}, received {agg_id}"
                        )
                    previous_id = agg_id
                    raw_rows.append(row)
                if len(page) < self.page_size:
                    break
                from_id = converted[-1][0] + 1
            cursor = window_end

        if not raw_rows:
            raise ValueError(f"Binance returned no aggregate trades for {market} gap")
        frame = pd.DataFrame(
            (
                {"ts_ms": timestamp, "price": price, "amount": amount, "side": side}
                for _agg_id, timestamp, price, amount, side in raw_rows
            ),
            columns=TRADE_SCHEMA,
        )
        digest = hashlib.sha256()
        for row in raw_rows:
            digest.update(("|".join(map(str, row)) + "\n").encode())
        return FetchedTape(
            symbol=market,
            start=start,
            end=end,
            frame=frame,
            first_agg_id=raw_rows[0][0],
            last_agg_id=raw_rows[-1][0],
            requests=requests,
            sha256=digest.hexdigest(),
        )


def _write_tape(
    tape: FetchedTape,
    data_root: Path,
    *,
    exchange: str,
) -> tuple[Path, ...]:
    validation = CandlePipeline(tape.symbol)
    for row in tape.frame.itertuples(index=False):
        validation.on_trade(
            datetime.fromtimestamp(int(row.ts_ms) / 1_000, tz=UTC),
            Decimal(str(row.price)),
            Decimal(str(row.amount)),
            str(row.side).lower() != "buy",
        )
    validation.advance_time(tape.end)

    frame = tape.frame.copy()
    frame["_day"] = pd.to_datetime(frame["ts_ms"], unit="ms", utc=True).dt.strftime(
        "%Y%m%d"
    )
    paths: list[Path] = []
    for day, chunk in frame.groupby("_day", sort=True):
        payload = chunk.drop(columns="_day").reset_index(drop=True)
        directory = shard_dir(data_root, tape.symbol, str(day), exchange)
        directory.mkdir(parents=True, exist_ok=True)
        first_ts = int(payload["ts_ms"].iloc[0])
        identity = hashlib.sha256(
            f"{tape.symbol}|{tape.start.isoformat()}|{tape.end.isoformat()}".encode()
        ).hexdigest()[:12]
        final = directory / f"{first_ts}-gapfill-{identity}.parquet"
        tmp = directory / f".{final.name}.tmp"
        payload.to_parquet(tmp, index=False)
        tmp.replace(final)
        paths.append(final)
    return tuple(paths)


def recover_storage_gaps(
    *,
    data_root: Path | str,
    candle_root: Path | str,
    gap_root: Path | str,
    exchange: str,
    symbols: list[str],
    fetcher: IntervalFetcher,
) -> RecoveryReport:
    """Fetch, replay and then close exact unrecovered storage-hole records."""
    data_path = Path(data_root)
    gap_store = GapParquetStore(gap_root)
    recovered: list[RecoveredGap] = []
    skipped: list[str] = []

    for symbol in symbols:
        records = gap_store.read(exchange, symbol)
        holes = [
            record
            for record in records
            if record.kind == GapKind.STORAGE_HOLE and not record.recovered
        ]
        if not holes:
            skipped.append(_market_id(symbol))
            continue
        for gap in holes:
            tape = fetcher.fetch(symbol, gap.start, gap.end)
            paths = _write_tape(tape, data_path, exchange=exchange)
            days = max(1, (datetime.now(UTC).date() - gap.start.date()).days + 1)
            candle_report: BootstrapReport = bootstrap_candles(
                data_path,
                candle_root,
                source_exchange=exchange,
                target_exchange=exchange,
                symbols=[symbol],
                days=days,
            )
            if candle_report.rejected:
                logger.warning(
                    "%s canonical replay skipped %d invalid/out-of-order rows from "
                    "the combined live lake; the fetched gap tape itself passed "
                    "strict value and aggregate-ID validation",
                    symbol,
                    candle_report.rejected,
                )
            proof = (
                f"recovered from Binance REST aggTrades; rows={tape.trades}; "
                f"agg_ids={tape.first_agg_id}-{tape.last_agg_id}; sha256={tape.sha256}; "
                f"unrelated_replay_rejections={candle_report.rejected}"
            )
            gap_store.upsert(
                (
                    replace(
                        gap,
                        recovered=True,
                        detail="; ".join(part for part in (gap.detail, proof) if part),
                    ),
                )
            )
            recovered.append(
                RecoveredGap(
                    symbol=tape.symbol,
                    gap_id=gap.gap_id,
                    start=gap.start.isoformat(),
                    end=gap.end.isoformat(),
                    trades=tape.trades,
                    first_agg_id=tape.first_agg_id,
                    last_agg_id=tape.last_agg_id,
                    requests=tape.requests,
                    sha256=tape.sha256,
                    shards=tuple(str(path) for path in paths),
                    candles=candle_report.candles,
                    unrelated_replay_rejections=candle_report.rejected,
                )
            )
    return RecoveryReport(exchange, tuple(recovered), tuple(skipped))


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(
        description="recover exact Binance USDM storage gaps from public aggTrades"
    )
    parser.add_argument(
        "--symbols",
        default="BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT",
    )
    parser.add_argument("--exchange", default="binanceusdm")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--candle-root", default="data/candles")
    parser.add_argument("--gap-root", default="data/gaps")
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=DEFAULT_REQUEST_INTERVAL_SECONDS,
    )
    parser.add_argument("--report", default="data/reports/binance_gap_recovery.json")
    args = parser.parse_args(argv)
    symbols = _csv(args.symbols)
    if not symbols:
        parser.error("--symbols must name at least one symbol")

    with BinanceAggTradeRest(
        request_interval_seconds=args.request_interval_seconds
    ) as fetcher:
        report = recover_storage_gaps(
            data_root=args.data_root,
            candle_root=args.candle_root,
            gap_root=args.gap_root,
            exchange=args.exchange,
            symbols=symbols,
            fetcher=fetcher,
        )
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = report_path.with_suffix(f"{report_path.suffix}.tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    tmp.replace(report_path)
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
