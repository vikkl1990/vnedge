"""Trial ledger — makes the multiple-testing count a query, not a memory.

A deflated Sharpe is only as honest as the ``n_trials`` fed to it, and that
number is exactly the thing a human cannot recall.  On 2026-08-19 a scanner
DSR was computed with ``n_trials=60`` estimated from memory; the ledger exists
so that never happens again.

It wraps :class:`ExperimentRecorder` with a scanner-shaped contract:

* every backtest of a configuration is recorded against a **window key** --
  the data it was measured on -- because trials only compete when they were
  scored on the same tape;
* the honest trial count for a window is then ``trial_count(window_key)``;
* and :func:`trial_sharpes` returns the per-trial Sharpes that DSR needs to
  estimate the dispersion of the search, straight from the recorded runs.

Records only.  Nothing here trades, promotes, or gates.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vnedge.research.experiment_recorder import ExperimentRecorder

RUN_NAME = "scanner_backtest"


def window_key(
    *, start_ms: int, end_ms: int, symbols: Sequence[str], timeframe: str = "5m"
) -> str:
    """Stable id for 'the data this trial was scored on'.

    Two configs are competing trials only if this key matches; a different
    window is a different question, not another attempt at the same one.
    """
    payload = {
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "symbols": sorted(str(s) for s in symbols),
        "timeframe": timeframe,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:12]
    span_days = round((end_ms - start_ms) / 86_400_000)
    return f"w{span_days}d_{digest}"


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@dataclass
class TrialLedger:
    """Append-and-query store of every scanner configuration that was scored."""

    recorder: ExperimentRecorder

    @classmethod
    def open(cls, root: str | Path = "research/experiments") -> "TrialLedger":
        return cls(recorder=ExperimentRecorder(root))

    # ------------------------------------------------------------- write
    def record(
        self,
        *,
        arm: str,
        window: str,
        params: Mapping[str, Any],
        metrics: Mapping[str, Any],
        symbols: Sequence[str],
        sharpe: float | None = None,
        note: str = "",
    ) -> str:
        """Record one scored configuration. Returns its run id."""
        run_id = self.recorder.start_run(
            RUN_NAME,
            tags={
                "arm": arm,
                "window": window,
                "git_sha": _git_sha(),
                "recorded_at": datetime.now(UTC).isoformat(),
                "note": note,
            },
        )
        self.recorder.log_params(run_id, {"arm": arm, "window": window,
                                          "symbols": sorted(symbols), **dict(params)})
        payload = dict(metrics)
        if sharpe is not None:
            payload["sharpe"] = float(sharpe)
        self.recorder.log_metrics(run_id, payload)
        self.recorder.set_status(run_id, "FINISHED")
        return run_id

    # -------------------------------------------------------------- read
    def _rows(self, window: str, *, arm: str | None = None) -> list[dict]:
        rows = self.recorder.search_records(
            name=RUN_NAME, tag=("window", window)
        )
        if arm is not None:
            rows = [r for r in rows if r.get("tags", {}).get("arm") == arm]
        return rows

    def trial_count(self, window: str, *, arm: str | None = None) -> int:
        """How many configurations have been scored on this window.

        This is the number DSR must be charged with -- including the variants
        that were discarded, which is precisely why it cannot come from memory.
        """
        return len(self._rows(window, arm=arm))

    def trial_sharpes(self, window: str, *, arm: str | None = None) -> list[float]:
        """Per-trial Sharpes on this window, for DSR's dispersion estimate."""
        out: list[float] = []
        for row in self._rows(window, arm=arm):
            value = row.get("metrics", {}).get("sharpe")
            if isinstance(value, (int, float)):
                out.append(float(value))
        return out

    def summary(self, window: str) -> dict:
        """Everything a report needs to state its own multiple-testing load."""
        rows = self._rows(window)
        by_arm: dict[str, int] = {}
        for row in rows:
            arm = str(row.get("tags", {}).get("arm", "?"))
            by_arm[arm] = by_arm.get(arm, 0) + 1
        return {
            "window": window,
            "trials": len(rows),
            "by_arm": by_arm,
            "sharpes": self.trial_sharpes(window),
            "git_shas": sorted({str(r.get("tags", {}).get("git_sha", "?")) for r in rows}),
        }
