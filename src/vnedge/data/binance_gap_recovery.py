"""Recover Binance USDM storage holes from the public aggregate-trade API.

This command is intentionally narrower than the research archive backfill:

* it reads unrecovered ``storage_hole`` records and may materialize the exact
  missing closed tail after a recorder restart;
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
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol, Self

import httpx
import pandas as pd  # type: ignore[import-untyped]

from vnedge.data.aggtrades_backfill import TRADE_SCHEMA, shard_dir
from vnedge.data.candles import (
    TF_SECONDS,
    Candle,
    CandleParquetStore,
    CandlePipeline,
    floor_time,
)
from vnedge.data.gaps import GapKind, GapParquetStore, GapRecord
from vnedge.data.lake_health import LakeHealthMonitor

logger = logging.getLogger(__name__)

BASE_URL = "https://fapi.binance.com/fapi/v1/aggTrades"
MAX_WINDOW = timedelta(hours=1)
DEFAULT_PAGE_SIZE = 1_000
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.55
REST_RETENTION_ERROR = -4166


def _utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _market_id(symbol: str) -> str:
    return symbol.split(":", 1)[0].replace("/", "").upper()


def _recovery_chunks(start: datetime, end: datetime) -> tuple[tuple[datetime, datetime], ...]:
    """Split a gap into independently durable REST recovery units."""
    chunks = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + MAX_WINDOW, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return tuple(chunks)


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
    frame["_day"] = pd.to_datetime(frame["ts_ms"], unit="ms", utc=True).dt.strftime("%Y%m%d")
    paths: list[Path] = []
    for day, chunk in frame.groupby("_day", sort=True):
        payload = chunk.drop(columns="_day").reset_index(drop=True)
        directory = shard_dir(data_root, tape.symbol, str(day), exchange)
        directory.mkdir(parents=True, exist_ok=True)
        start_ms = int(tape.start.timestamp() * 1_000)
        end_ms = int(tape.end.timestamp() * 1_000)
        identity = hashlib.sha256(
            f"{tape.symbol}|{tape.start.isoformat()}|{tape.end.isoformat()}".encode()
        ).hexdigest()[:12]
        # The exact requested interval is encoded in the filename so a future
        # full bootstrap can give this proven REST tape precedence over a
        # recorder's partial, overlapping shard from before the repair.
        final = directory / f"{start_ms}-gapfill-{end_ms}-{identity}.parquet"
        tmp = directory / f".{final.name}.tmp"
        payload.to_parquet(tmp, index=False)
        tmp.replace(final)
        paths.append(final)
    return tuple(paths)


def _replay_fetched_tape(
    tape: FetchedTape,
    store: CandleParquetStore,
) -> int:
    """Upsert candles from only the verified interval, then repair parents.

    Replaying the combined live lake here is unsafe: a recorder restart may
    have captured the final minutes of the otherwise missing hour, and the
    authoritative REST gapfill necessarily overlaps those partial rows.
    Replaying only ``tape`` prevents double-counted volume.  Constructing a
    store-backed pipeline afterwards deterministically rebuilds any now-
    complete higher-timeframe buckets from persisted child bars.
    """
    captured: list[Candle] = []
    pipeline = CandlePipeline(tape.symbol, subscribers=(captured.append,))
    for row in tape.frame.itertuples(index=False):
        pipeline.on_trade(
            datetime.fromtimestamp(int(row.ts_ms) / 1_000, tz=UTC),
            Decimal(str(row.price)),
            Decimal(str(row.amount)),
            str(row.side).lower() != "buy",
        )
    pipeline.advance_time(tape.end)
    if captured:
        store.upsert(captured)
        # Constructor recovery is deliberately delta-only: it fills missing
        # parent buckets but never overwrites existing authoritative candles.
        CandlePipeline(tape.symbol, store=store)
    return len(captured)


def _canonical_gap_covered(
    store: CandleParquetStore,
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe: str = "1h",
) -> bool:
    """True only when every missing target bucket has a canonical bar."""
    try:
        step = timedelta(seconds=TF_SECONDS[timeframe])
    except KeyError as exc:
        raise ValueError(f"unsupported recovery coverage timeframe: {timeframe}") from exc
    expected: set[datetime] = set()
    cursor = start
    while cursor < end:
        expected.add(cursor)
        cursor += step
    if not expected or cursor != end:
        return False
    present = {
        candle.open_time
        for candle in store.read(_market_id(symbol), timeframe)
        if candle.is_closed and start <= candle.open_time < end
    }
    return expected <= present


def _rest_retention_rejection(exc: httpx.HTTPStatusError) -> bool:
    try:
        payload = exc.response.json()
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("code") == REST_RETENTION_ERROR


def recover_storage_gaps(
    *,
    data_root: Path | str,
    candle_root: Path | str,
    gap_root: Path | str,
    exchange: str,
    symbols: list[str],
    fetcher: IntervalFetcher,
    recover_closed_tail: bool = False,
    tail_timeframe: str = "1h",
    now: datetime | None = None,
) -> RecoveryReport:
    """Fetch, replay and then close exact unrecovered storage-hole records.
    ``recover_closed_tail`` is used by the long-running repair worker.  A
    recorder restart during an hour cannot reconstruct the already elapsed
    part of that hour from its live websocket.  Once that hour is closed, it
    is a provable missing interval even though there is not yet a later candle
    with which the ordinary interior-hole detector can bracket it.

    The tail is bounded to completed UTC hours and persisted as an ordinary
    ``storage_hole`` before recovery, so it follows the same strict aggregate
    trade-ID proof and audit path as every other repair.
    """
    data_path = Path(data_root)
    gap_store = GapParquetStore(gap_root)
    candle_store = CandleParquetStore(candle_root, exchange=exchange)
    recovered: list[RecoveredGap] = []
    skipped: list[str] = []
    moment = _utc(now or datetime.now(UTC), label="recovery clock")
    if tail_timeframe not in {"5m", "1h"}:
        raise ValueError("tail_timeframe must be '5m' or '1h'")
    completed_tail_end = floor_time(moment, tail_timeframe)

    for symbol in symbols:
        records = gap_store.read(exchange, symbol)
        if recover_closed_tail:
            closed_tail = sorted(
                (
                    candle
                    for candle in candle_store.read(
                        _market_id(symbol), tail_timeframe
                    )
                    if candle.is_closed and candle.close_time <= completed_tail_end
                ),
                key=lambda candle: candle.open_time,
            )
            step = timedelta(seconds=TF_SECONDS[tail_timeframe])
            known_ids = {record.gap_id for record in records}
            interior_holes: list[GapRecord] = []
            for previous, current in pairwise(closed_tail):
                if current.open_time - previous.open_time <= step:
                    continue
                hole_start = previous.close_time
                hole_end = current.open_time
                hole_id = (
                    f"interior-{tail_timeframe}-{exchange}-{_market_id(symbol)}-"
                    f"{hole_start:%Y%m%d%H%M}-{hole_end:%Y%m%d%H%M}"
                )
                if hole_id in known_ids:
                    continue
                interior_holes.append(
                    GapRecord(
                        symbol=_market_id(symbol),
                        exchange=exchange,
                        kind=GapKind.STORAGE_HOLE,
                        start=hole_start,
                        end=hole_end,
                        detected_at=moment,
                        detail=(
                            "interior canonical hole discovered by recovery worker; "
                            f"coverage_timeframe={tail_timeframe}"
                        ),
                        gap_id=hole_id,
                    )
                )
                known_ids.add(hole_id)
            if interior_holes:
                gap_store.upsert(interior_holes)
                records.extend(interior_holes)

            latest_close = max(
                (candle.close_time for candle in closed_tail),
                default=None,
            )
            if latest_close is not None and latest_close < completed_tail_end:
                tail_id = (
                    f"tail-{tail_timeframe}-{exchange}-{_market_id(symbol)}-"
                    f"{latest_close:%Y%m%d%H%M}-{completed_tail_end:%Y%m%d%H%M}"
                )
                already_recorded = any(record.gap_id == tail_id for record in records)
                if not already_recorded:
                    tail = GapRecord(
                        symbol=_market_id(symbol),
                        exchange=exchange,
                        kind=GapKind.STORAGE_HOLE,
                        start=latest_close,
                        end=completed_tail_end,
                        detected_at=moment,
                        detail=(
                            "closed canonical tail missing after recorder restart; "
                            "materialized by recovery worker; "
                            f"coverage_timeframe={tail_timeframe}"
                        ),
                        gap_id=tail_id,
                    )
                    gap_store.upsert((tail,))
                    records.append(tail)
        holes = [
            record
            for record in records
            if record.kind == GapKind.STORAGE_HOLE and not record.recovered
        ]
        if not holes:
            skipped.append(_market_id(symbol))
            continue
        grouped: dict[tuple[datetime, datetime], list[GapRecord]] = {}
        for gap in holes:
            grouped.setdefault((gap.start, gap.end), []).append(gap)
        for (start, end), duplicate_records in grouped.items():
            gap = duplicate_records[-1]
            coverage_timeframe = (
                "5m" if "coverage_timeframe=5m" in gap.detail else "1h"
            )
            if _canonical_gap_covered(
                candle_store,
                symbol,
                start,
                end,
                coverage_timeframe,
            ):
                proof = (
                    "recovered: canonical closed "
                    f"{coverage_timeframe} coverage already present"
                )
                gap_store.upsert(
                    replace(
                        record,
                        recovered=True,
                        detail="; ".join(part for part in (record.detail, proof) if part),
                    )
                    for record in duplicate_records
                )
                skipped.append(f"{_market_id(symbol)}:{start.isoformat()}:covered")
                continue
            chunk_failed = False
            recovered_chunks = 0
            for chunk_start, chunk_end in _recovery_chunks(start, end):
                if _canonical_gap_covered(
                    candle_store,
                    symbol,
                    chunk_start,
                    chunk_end,
                    coverage_timeframe,
                ):
                    continue
                try:
                    tape = fetcher.fetch(symbol, chunk_start, chunk_end)
                except httpx.HTTPStatusError as exc:
                    if not _rest_retention_rejection(exc):
                        raise
                    # Binance REST is a recent-2-day source. The daily Vision
                    # worker owns older intervals; one old gap must not prevent
                    # later, recoverable gaps from being attempted.
                    logger.warning(
                        "%s gap %s..%s is outside REST retention; awaiting Vision",
                        symbol,
                        chunk_start.isoformat(),
                        chunk_end.isoformat(),
                    )
                    skipped.append(
                        f"{_market_id(symbol)}:{chunk_start.isoformat()}:vision"
                    )
                    chunk_failed = True
                    break
                paths = _write_tape(tape, data_path, exchange=exchange)
                candle_count = _replay_fetched_tape(tape, candle_store)
                if not _canonical_gap_covered(
                    candle_store,
                    symbol,
                    chunk_start,
                    chunk_end,
                    coverage_timeframe,
                ):
                    logger.error(
                        "%s recovery replay did not produce complete closed %s "
                        "coverage for %s..%s; gap remains open",
                        symbol,
                        coverage_timeframe,
                        chunk_start.isoformat(),
                        chunk_end.isoformat(),
                    )
                    skipped.append(
                        f"{_market_id(symbol)}:{chunk_start.isoformat()}:unproven"
                    )
                    chunk_failed = True
                    break
                recovered_chunks += 1
                logger.info(
                    "%s recovered %s..%s (%d trades, %d requests)",
                    symbol,
                    chunk_start.isoformat(),
                    chunk_end.isoformat(),
                    tape.trades,
                    tape.requests,
                )
                recovered.append(
                    RecoveredGap(
                        symbol=tape.symbol,
                        gap_id=(
                            gap.gap_id
                            if start == chunk_start and end == chunk_end
                            else f"{gap.gap_id}:{chunk_start:%Y%m%d%H%M}"
                        ),
                        start=chunk_start.isoformat(),
                        end=chunk_end.isoformat(),
                        trades=tape.trades,
                        first_agg_id=tape.first_agg_id,
                        last_agg_id=tape.last_agg_id,
                        requests=tape.requests,
                        sha256=tape.sha256,
                        shards=tuple(str(path) for path in paths),
                        candles=candle_count,
                        unrelated_replay_rejections=0,
                    )
                )
            if chunk_failed or not _canonical_gap_covered(
                candle_store,
                symbol,
                start,
                end,
                coverage_timeframe,
            ):
                continue
            proof = (
                "recovered from Binance REST aggTrades in independently durable "
                f"chunks; newly_fetched_chunks={recovered_chunks}; "
                "unrelated_replay_rejections=0"
            )
            gap_store.upsert(
                replace(
                    record,
                    recovered=True,
                    detail="; ".join(part for part in (record.detail, proof) if part),
                )
                for record in duplicate_records
            )
    return RecoveryReport(exchange, tuple(recovered), tuple(skipped))


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _tails_current(
    candle_store: CandleParquetStore,
    symbols: list[str],
    timeframe: str,
    *,
    now: datetime,
) -> bool:
    expected_close = floor_time(now, timeframe)
    for symbol in symbols:
        latest_close = max(
            (
                candle.close_time
                for candle in candle_store.read(_market_id(symbol), timeframe)
                if candle.is_closed
            ),
            default=None,
        )
        if latest_close is None or latest_close < expected_close:
            return False
    return True


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
        "--tail-timeframe",
        choices=("5m", "1h"),
        default="1h",
        help="closed-tail recovery granularity; scanner startup uses 5m",
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=DEFAULT_REQUEST_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--max-tail-passes",
        type=int,
        default=3,
        help="bounded rechecks so a long initial repair also heals elapsed 5m delta",
    )
    parser.add_argument("--report", default="data/reports/binance_gap_recovery.json")
    args = parser.parse_args(argv)
    symbols = _csv(args.symbols)
    if not symbols:
        parser.error("--symbols must name at least one symbol")
    if args.max_tail_passes < 1:
        parser.error("--max-tail-passes must be >= 1")

    with BinanceAggTradeRest(request_interval_seconds=args.request_interval_seconds) as fetcher:
        recovered: list[RecoveredGap] = []
        skipped: list[str] = []
        candle_store = CandleParquetStore(args.candle_root, exchange=args.exchange)
        for pass_number in range(1, args.max_tail_passes + 1):
            pass_report = recover_storage_gaps(
                data_root=args.data_root,
                candle_root=args.candle_root,
                gap_root=args.gap_root,
                exchange=args.exchange,
                symbols=symbols,
                fetcher=fetcher,
                recover_closed_tail=True,
                tail_timeframe=args.tail_timeframe,
            )
            recovered.extend(pass_report.recovered)
            skipped.extend(pass_report.skipped_symbols)
            if _tails_current(
                candle_store,
                symbols,
                args.tail_timeframe,
                now=datetime.now(UTC),
            ):
                break
            logger.info(
                "closed %s tail advanced during recovery; convergence pass %d/%d",
                args.tail_timeframe,
                pass_number + 1,
                args.max_tail_passes,
            )
        report = RecoveryReport(args.exchange, tuple(recovered), tuple(skipped))
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = report_path.with_suffix(f"{report_path.suffix}.tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    tmp.replace(report_path)
    # Recovery and readiness are one workflow. Do not leave the dashboard on
    # a stale DEGRADED result until the recorder's next 15-minute monitor pass.
    # This scan remains fail-closed: any remaining hole or stale tail keeps the
    # published lake status non-healthy.
    LakeHealthMonitor(
        exchange=args.exchange,
        symbols=[_market_id(symbol) for symbol in symbols],
        candle_root=Path(args.candle_root),
        gap_root=Path(args.gap_root),
    ).check_once()
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
