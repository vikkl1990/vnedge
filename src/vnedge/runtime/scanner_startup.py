"""Fail-closed scanner entrypoint with restart-safe prerequisite recovery.

Docker restart policies restart the container command, but they do not rerun a
separate one-shot dependency that exited successfully during an earlier boot.
This entrypoint therefore owns the prerequisite sequence and replaces itself
with the actual scanner runtime only after every command succeeds.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = "BTC/USDT:USDT,ETH/USDT:USDT"
MINIMUM_ARCHIVE_DAYS = 9


def _archive_days(environ: Mapping[str, str]) -> int:
    raw = environ.get("VISION_BACKFILL_DAYS", str(MINIMUM_ARCHIVE_DAYS))
    try:
        return max(MINIMUM_ARCHIVE_DAYS, int(raw))
    except ValueError as exc:
        raise ValueError("VISION_BACKFILL_DAYS must be an integer") from exc


def prerequisite_commands(
    environ: Mapping[str, str] = os.environ,
) -> tuple[tuple[str, ...], ...]:
    """Return the audited, shell-free startup command sequence."""
    python = sys.executable
    symbols = environ.get("SCANNER_PREREQ_SYMBOLS", DEFAULT_SYMBOLS).strip()
    if not symbols:
        raise ValueError("SCANNER_PREREQ_SYMBOLS must not be empty")
    days = str(_archive_days(environ))
    return (
        (
            python,
            "-m",
            "vnedge.data.aggtrades_backfill",
            "--symbols",
            symbols,
            "--days",
            days,
            "--data-root",
            "data",
            "--exchange",
            "binanceusdm_hist",
            "--concurrency",
            "1",
        ),
        (
            python,
            "-m",
            "vnedge.data.candle_bootstrap",
            "--symbols",
            symbols,
            "--days",
            days,
            "--data-root",
            "data",
            "--candle-root",
            "data/candles",
            "--source-exchange",
            "binanceusdm_hist",
            "--target-exchange",
            "binanceusdm",
        ),
        (
            python,
            "-m",
            "vnedge.data.binance_gap_recovery",
            "--symbols",
            symbols,
            "--data-root",
            "data",
            "--candle-root",
            "data/candles",
            "--gap-root",
            "data/gaps",
            "--tail-timeframe",
            "5m",
            "--max-tail-passes",
            "3",
            "--report",
            "data/reports/scanner_prereq_gap_recovery.json",
        ),
        (
            python,
            "-m",
            "vnedge.data.scanner_prereq",
            "--symbols",
            symbols,
            "--candle-root",
            "data/candles",
            "--exchange",
            "binanceusdm",
            "--report",
            "data/reports/scanner_prerequisites.json",
        ),
    )


def run_prerequisites(commands: Sequence[Sequence[str]]) -> None:
    for index, command in enumerate(commands, start=1):
        logger.info("scanner startup prerequisite %d/%d: %s", index, len(commands), command[2])
        subprocess.run(tuple(command), check=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_prerequisites(prerequisite_commands())
    logger.info("scanner prerequisites current; starting multi-lane runtime")
    os.execv(
        sys.executable,
        (sys.executable, "-m", "vnedge.runtime.multi_lane_shadow"),
    )
    return 1  # pragma: no cover - execv replaces the process


if __name__ == "__main__":
    raise SystemExit(main())
