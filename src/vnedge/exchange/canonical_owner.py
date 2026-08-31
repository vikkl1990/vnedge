"""Single ownership domain for live public trades and canonical maintenance.

The live recorder holds the venue writer lease for its full lifetime.  Repair
and bootstrap commands run only as bounded children that inherit and verify
that exact locked descriptor.  Scanner and recovery containers therefore
cannot mutate the canonical lake concurrently with the market-data owner.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from vnedge.exchange.tick_recorder import DeltaTickRecorder, TickRecorder
from vnedge.exchange.writer_lease import CanonicalWriterLease
from vnedge.runtime.scanner_startup import (
    prerequisite_commands,
    run_prerequisites,
    write_health,
)

logger = logging.getLogger(__name__)


def _positive_seconds(environ: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


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
            recorder = DeltaTickRecorder(
                list(symbols),
                data_root,
                exchange_id=exchange,
                candle_root=candle_root,
                trades_only=True,
            )
        else:
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
            tasks = (recorder_task,)
            await recorder_task
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
