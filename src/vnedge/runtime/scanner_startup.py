"""Fail-closed scanner entrypoint with restart-safe prerequisite recovery.

Docker restart policies restart the container command, but they do not rerun a
separate one-shot dependency that exited successfully during an earlier boot.
This entrypoint launches a retrying recovery worker, then immediately replaces
itself with the read-only runtime.  New scanner arms remain fail-closed behind
the worker's atomic health artifact, while the operator UI stays available.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from vnedge.exchange.writer_lease import INHERITED_WRITER_LEASE_FD

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = "BTC/USDT:USDT,ETH/USDT:USDT"
MINIMUM_ARCHIVE_DAYS = 2
DEFAULT_HEALTH_PATH = Path("data/reports/scanner_startup_health.json")


def _active_requirements(environ: Mapping[str, str]) -> Mapping[str, int]:
    from vnedge.data.scanner_prereq import DEFAULT_REQUIREMENTS, requirements_from_roster

    roster = str(environ.get("MULTI_LANE_SHADOW_OBSERVE_ROSTER_PATH") or "").strip()
    return requirements_from_roster(roster) if roster else DEFAULT_REQUIREMENTS


def _archive_days(environ: Mapping[str, str]) -> int:
    from vnedge.data.candles import TF_SECONDS

    requirements = _active_requirements(environ)
    computed = max(
        MINIMUM_ARCHIVE_DAYS,
        max(
            math.ceil(required * TF_SECONDS[timeframe] / 86_400) + 2
            for timeframe, required in requirements.items()
        ),
    )
    raw = environ.get("VISION_BACKFILL_DAYS", str(computed))
    try:
        return max(computed, int(raw))
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
    roster = str(environ.get("MULTI_LANE_SHADOW_OBSERVE_ROSTER_PATH") or "").strip()
    commands: tuple[tuple[str, ...], ...] = (
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
    if roster:
        commands = commands[:-1] + (commands[-1] + ("--roster", roster),)
    return commands


def run_prerequisites(
    commands: Sequence[Sequence[str]],
    *,
    inherited_lease_fd: int | None = None,
) -> None:
    inherited = str(
        inherited_lease_fd
        if inherited_lease_fd is not None
        else os.environ.get(INHERITED_WRITER_LEASE_FD, "")
    ).strip()
    pass_fds: tuple[int, ...] = ()
    child_env = os.environ.copy()
    if inherited:
        try:
            pass_fds = (int(inherited),)
        except ValueError as exc:
            raise ValueError("canonical writer lease descriptor must be an integer") from exc
        child_env[INHERITED_WRITER_LEASE_FD] = inherited
    for index, command in enumerate(commands, start=1):
        logger.info("scanner startup prerequisite %d/%d: %s", index, len(commands), command[2])
        subprocess.run(
            tuple(command),
            check=True,
            env=child_env,
            pass_fds=pass_fds,
        )


def _health_path(environ: Mapping[str, str] = os.environ) -> Path:
    return Path(environ.get("SCANNER_PREREQ_HEALTH_PATH", str(DEFAULT_HEALTH_PATH)))


def write_health(
    status: str,
    *,
    detail: str,
    environ: Mapping[str, str] = os.environ,
) -> None:
    path = _health_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "detail": detail,
        "checked_at": datetime.now(UTC).isoformat(),
        "arms_allowed": status == "ready",
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def recover_until_ready(
    environ: Mapping[str, str] = os.environ,
    *,
    retry_seconds: float = 60.0,
) -> int:
    """Retry prerequisites while the read-only runtime remains available."""
    attempt = 0
    while True:
        attempt += 1
        write_health("recovering", detail=f"attempt {attempt}", environ=environ)
        try:
            run_prerequisites(prerequisite_commands(environ))
        except Exception as exc:
            logger.exception("scanner prerequisite recovery attempt %d failed", attempt)
            write_health(
                "retrying",
                detail=f"{type(exc).__name__}: {exc}",
                environ=environ,
            )
            time.sleep(max(1.0, retry_seconds))
            continue
        write_health("ready", detail=f"completed on attempt {attempt}", environ=environ)
        logger.info("scanner prerequisites current; new shadow arms enabled")
        return 0


def archive_retired_lane_artifacts(environ: Mapping[str, str] = os.environ) -> None:
    """Move no-longer-configured lane evidence out of the active journal root.

    The move is recoverable (manifest + timestamped archive) and roster scoped.
    Keeping retired journals beside active lanes makes the health auditor report
    them as ORPHAN on every restart even though no orphan process exists.
    """
    from vnedge.runtime.multi_lane_shadow import desired_lane_specs
    from vnedge.runtime.orphan_lane_archive import archive_orphan_lane_artifacts

    journal_dir = Path(environ.get("MULTI_LANE_JOURNAL_DIR", "logs/paper_trials"))
    plan = archive_orphan_lane_artifacts(
        journal_dir,
        desired=desired_lane_specs(environ),
        apply=True,
    )
    if plan.applied:
        logger.info(
            "archived %d retired lane artifact(s) for %d lane(s) under %s",
            len(plan.files),
            len(plan.lane_ids),
            plan.archive_dir,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover-only", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.recover_only:
        return recover_until_ready()
    # Keep recoverable journal moves single-threaded and ahead of the runtime;
    # the background worker only owns canonical data repair/readiness.
    archive_retired_lane_artifacts()
    maintenance_owner = (
        str(os.environ.get("VNEDGE_CANONICAL_MAINTENANCE_OWNER", "scanner_startup")).strip().lower()
    )
    if maintenance_owner == "pulse_recorder":
        logger.info(
            "canonical prerequisite repair is owned by pulse-recorder; "
            "starting runtime behind its fail-closed health artifact"
        )
    elif maintenance_owner == "scanner_startup":
        write_health("recovering", detail="startup worker launching")
        subprocess.Popen(
            (sys.executable, "-m", "vnedge.runtime.scanner_startup", "--recover-only"),
            close_fds=True,
        )
        logger.info("starting read-only runtime while scanner prerequisites recover")
    else:
        raise ValueError(
            "VNEDGE_CANONICAL_MAINTENANCE_OWNER must be pulse_recorder or scanner_startup"
        )
    os.execv(
        sys.executable,
        (sys.executable, "-m", "vnedge.runtime.multi_lane_shadow"),
    )
    return 1  # pragma: no cover - execv replaces the process


if __name__ == "__main__":
    raise SystemExit(main())
