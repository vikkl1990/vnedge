from __future__ import annotations

import subprocess
import sys

import pytest

from vnedge.runtime.scanner_startup import prerequisite_commands, run_prerequisites


def test_prerequisite_commands_are_restart_safe_and_ordered() -> None:
    commands = prerequisite_commands(
        {
            "VISION_BACKFILL_DAYS": "2",
            "SCANNER_PREREQ_SYMBOLS": "BTC/USDT:USDT,ETH/USDT:USDT",
        }
    )

    assert [command[2] for command in commands] == [
        "vnedge.data.aggtrades_backfill",
        "vnedge.data.candle_bootstrap",
        "vnedge.data.binance_gap_recovery",
        "vnedge.data.scanner_prereq",
    ]
    assert all(command[0] == sys.executable for command in commands)
    assert commands[0][commands[0].index("--days") + 1] == "9"
    assert "--max-tail-passes" in commands[2]


def test_prerequisites_stop_at_first_failed_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(command, *, check):
        calls.append(tuple(command))
        assert check is True
        if len(calls) == 2:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", fake_run)
    commands = (("python", "-m", "one"), ("python", "-m", "two"), ("python", "-m", "three"))

    with pytest.raises(subprocess.CalledProcessError):
        run_prerequisites(commands)

    assert [command[2] for command in calls] == ["one", "two"]


def test_invalid_archive_days_fail_closed() -> None:
    with pytest.raises(ValueError, match="VISION_BACKFILL_DAYS"):
        prerequisite_commands({"VISION_BACKFILL_DAYS": "invalid"})
