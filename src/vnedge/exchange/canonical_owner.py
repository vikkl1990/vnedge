"""Single ownership domain for live public trades and canonical maintenance.

The live recorder holds the venue writer lease for its full lifetime.  Repair
and bootstrap commands run only as bounded children that inherit and verify
that exact locked descriptor.  Scanner and recovery containers therefore
cannot mutate the canonical lake concurrently with the market-data owner.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from vnedge.data.candle_bootstrap import bootstrap_candles
from vnedge.data.candles import CandleParquetStore
from vnedge.exchange.tick_recorder import DeltaTickRecorder, TickRecorder
from vnedge.exchange.writer_lease import CanonicalWriterLease
from vnedge.exchange.writer_lease import INHERITED_WRITER_LEASE_FD
from vnedge.runtime.scanner_startup import (
    prerequisite_commands,
    run_prerequisites,
    write_health,
)

logger = logging.getLogger(__name__)

_CANONICAL_IDENTITY_MIGRATION_VERSION = 2
_BINANCE_CANONICAL_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")


def _positive_seconds(environ: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative_int(environ: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def _bootstrap_delta_tail(
    *,
    symbols: Sequence[str],
    data_root: Path,
    candle_root: Path,
    environ: Mapping[str, str],
) -> None:
    """Repair recent Delta candle seams before the live writer starts.

    The owner already holds the canonical writer lease here, so the repair
    and recorder never overlap. A zero-day setting explicitly disables the
    bounded replay; the default covers reconnect/startup seams without
    pretending to be a historical Delta trade backfill.
    """

    days = _nonnegative_int(environ, "VNEDGE_DELTA_BOOTSTRAP_DAYS", 7)
    if days == 0:
        return
    report = bootstrap_candles(
        data_root,
        candle_root,
        source_exchange="delta_india",
        target_exchange="delta_india",
        symbols=symbols,
        days=days,
    )
    logger.info(
        "Delta canonical tail bootstrap: %s symbols, %s shards, %s trades, "
        "%s candles, %s rejected, %s existing minutes skipped",
        report.symbols,
        report.shards,
        report.trades,
        report.candles,
        report.rejected,
        report.skipped_existing_minutes,
    )


def _attest_binance_legacy_identity_once(
    *,
    symbols: Sequence[str],
    data_root: Path,
    candle_root: Path,
    environ: Mapping[str, str],
) -> None:
    """Run the reviewed pre-schema Binance provenance migration once.

    Old Binance partitions were produced from the exact aggTrade tape but did
    not persist provenance columns.  Normal writes deliberately disclose them
    as partial; only the canonical owner, while holding the writer lease, may
    make the one-time attestation.  Delta is excluded because its historical
    context may legitimately be official OHLC rather than trade-derived.
    """
    configured = str(
        environ.get(
            "VNEDGE_CANONICAL_IDENTITY_MIGRATION_MARKER",
            data_root / "state" / "canonical_identity_v2_binanceusdm.json",
        )
    ).strip()
    marker = Path(configured)
    if marker.exists():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"canonical identity migration marker unreadable: {marker}") from exc
        if int(payload.get("version") or 0) == _CANONICAL_IDENTITY_MIGRATION_VERSION:
            return
        raise RuntimeError(f"unsupported canonical identity migration marker: {marker}")

    store = CandleParquetStore(candle_root, exchange="binanceusdm")
    rows = 0
    for symbol in symbols:
        for timeframe in _BINANCE_CANONICAL_TIMEFRAMES:
            rows += store.stamp_legacy_partitions(
                symbol,
                timeframe,
                source="canonical_tick_lake",
            )
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(marker.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": _CANONICAL_IDENTITY_MIGRATION_VERSION,
                "exchange": "binanceusdm",
                "symbols": list(symbols),
                "timeframes": list(_BINANCE_CANONICAL_TIMEFRAMES),
                "rows_attested": rows,
                "completed_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)
    logger.info("attested %s legacy Binance canonical rows; marker=%s", rows, marker)


def maintenance_commands(environ: Mapping[str, str], *, full: bool) -> tuple[tuple[str, ...], ...]:
    """Return the immutable owner-run maintenance command set.

    Full cycles refresh the distinct historical tape before rebuilding the
    canonical lake.  Tail cycles only recover exact gaps and re-evaluate the
    scanner prerequisites.
    """
    commands = prerequisite_commands(environ)
    return commands if full else commands[2:]


def _run_maintenance_cycle(
    environ: Mapping[str, str],
    *,
    full: bool,
    lease_fd: int,
) -> None:
    label = "full" if full else "tail"
    write_health(
        "recovering",
        detail=f"canonical owner {label} maintenance",
        environ=environ,
    )
    try:
        run_prerequisites(
            maintenance_commands(environ, full=full),
            inherited_lease_fd=lease_fd,
        )
    except Exception as exc:
        write_health(
            "retrying",
            detail=f"canonical owner {label}: {type(exc).__name__}: {exc}",
            environ=environ,
        )
        raise
    write_health(
        "ready",
        detail=f"canonical owner {label} maintenance complete",
        environ=environ,
    )


async def _maintenance_loop(
    environ: Mapping[str, str],
    *,
    lease_fd: int,
) -> None:
    tail_interval = _positive_seconds(
        environ, "VNEDGE_CANONICAL_TAIL_REPAIR_INTERVAL_SECONDS", 900.0
    )
    full_interval = _positive_seconds(
        environ, "VNEDGE_CANONICAL_FULL_REPAIR_INTERVAL_SECONDS", 86_400.0
    )
    retry_interval = _positive_seconds(environ, "VNEDGE_CANONICAL_REPAIR_RETRY_SECONDS", 60.0)
    next_full = 0.0
    while True:
        full = time.monotonic() >= next_full
        try:
            await asyncio.to_thread(
                _run_maintenance_cycle,
                environ,
                full=full,
                lease_fd=lease_fd,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("canonical owner maintenance failed; arms remain blocked")
            await asyncio.sleep(retry_interval)
            continue
        if full:
            next_full = time.monotonic() + full_interval
        await asyncio.sleep(tail_interval)


async def _delta_maintenance_loop(data_root: Path, candle_root: Path, *,
                                  symbols: Sequence[str], environ: Mapping[str, str],
                                  lease_fd: int) -> None:
    from vnedge.data.delta_lake_repair import _atomic, repair_delta_lake

    interval = _positive_seconds(environ, "VNEDGE_DELTA_REPAIR_INTERVAL_SECONDS", 900)
    authority = {**environ, INHERITED_WRITER_LEASE_FD: str(lease_fd)}
    # Record first; never spend a historical replay window disconnected.
    await asyncio.sleep(10)
    while True:
        try:
            report = await asyncio.to_thread(
                repair_delta_lake, data_root, candle_root, symbols=tuple(symbols),
                apply=True, environ=authority)
            _atomic(data_root / "reports/delta_lake_repair.json", json.dumps(report, sort_keys=True).encode())
            logger.info("Delta quality-aware repair: %s", json.dumps(report["symbols"], sort_keys=True))
        except Exception as exc:
            logger.exception("Delta repair failed; no readiness claimed")
            _atomic(data_root / "reports/delta_lake_repair.json", json.dumps({
                "generated_at": datetime.now(UTC).isoformat(), "status": "ERROR",
                "reason": f"{type(exc).__name__}:{exc}", "can_trade": False}).encode())
        await asyncio.sleep(interval)


async def run_owner(
    *,
    exchange: str,
    symbols: Sequence[str],
    data_root: Path,
    candle_root: Path,
    environ: Mapping[str, str] = os.environ,
) -> None:
    """Run the live recorder and all canonical maintenance under one lease."""
    if exchange not in {"binanceusdm", "delta_india"}:
        raise ValueError("canonical owner supports binanceusdm or delta_india")
    if not symbols:
        raise ValueError("canonical owner requires at least one symbol")
    lease = CanonicalWriterLease(data_root, exchange).acquire()
    tasks: tuple[asyncio.Task[None], ...] = ()
    try:
        if exchange == "delta_india":
            # Raw presence cannot attest historical completeness. The Delta
            # repair worker below replaces the old trust-all startup replay.
            recorder = DeltaTickRecorder(
                list(symbols),
                data_root,
                exchange_id=exchange,
                candle_root=candle_root,
                levels=1,
                # One Delta socket owns the immutable trade and L1 tapes.
                # Recorder BBO is raw audit/replay input; lane-consumed quote
                # evidence remains the only acceptance-parity authority.
                trades_only=False,
            )
        else:
            await asyncio.to_thread(
                _attest_binance_legacy_identity_once,
                symbols=symbols,
                data_root=data_root,
                candle_root=candle_root,
                environ=environ,
            )
            recorder = TickRecorder(
                exchange,
                list(symbols),
                data_root,
                candle_root=candle_root,
                trades_only=True,
            )
        recorder_task = asyncio.create_task(
            recorder.run(acquire_writer_lease=False),
            name="canonical-owner-recorder",
        )
        # Historical canonical repair currently has a Binance-specific exact
        # aggTrade/Archive implementation. Delta owns a separate live trade
        # tape and must not run those commands against BTCUSD/ETHUSD. Its
        # recorder still holds the same process-lifetime writer lease and
        # produces the exact forward canonical ladder used by Delta lanes.
        if exchange == "delta_india":
            maintenance_task = asyncio.create_task(
                _delta_maintenance_loop(data_root, candle_root, symbols=symbols,
                                        environ=environ, lease_fd=lease.fileno),
                name="delta-canonical-maintenance")
            tasks = (recorder_task, maintenance_task)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise RuntimeError("Delta canonical recorder exited unexpectedly")
        maintenance_task = asyncio.create_task(
            _maintenance_loop(environ, lease_fd=lease.fileno),
            name="canonical-owner-maintenance",
        )
        tasks = (recorder_task, maintenance_task)
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise RuntimeError("canonical owner task exited unexpectedly")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        lease.release()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange", default="binanceusdm")
    parser.add_argument(
        "--symbols",
        default="BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT",
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--candle-root", default="data/candles")
    args = parser.parse_args(argv)
    symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
    asyncio.run(
        run_owner(
            exchange=args.exchange,
            symbols=symbols,
            data_root=Path(args.data_root),
            candle_root=Path(args.candle_root),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
