"""Decision journal — the append-only WAL (docs/DESIGN.md §4).

Every signal, risk decision, intent, submission, ack, and error is written
here BEFORE the next step happens. After a crash, this file is the
deterministic baseline for reconstructing what the bot was doing.

The journal-unavailable rule: if a record cannot be written, the journal
marks itself unavailable and the order manager rejects all risk-increasing
orders (reduce-only exits remain allowed). If we can't record what we're
doing, we don't create new risk.

Writes are line-buffered JSONL with flush+fsync per record — at our order
frequency, durability beats throughput by an enormous margin.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DecisionJournal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._available = True
        self._recovery_degraded = False
        self._recovery_error = ""
        self._quarantine_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Probe writability at startup, not at first order.
            with open(self.path, "a", encoding="utf-8"):
                pass
            self._recover_valid_prefix()
        except OSError as exc:
            self._mark_unavailable(f"journal probe failed: {exc}")

    @property
    def available(self) -> bool:
        return self._available

    @property
    def recovery_degraded(self) -> bool:
        """True when startup found and quarantined a corrupt journal tail.

        The valid prefix remains readable, but callers must not create new risk
        until venue reconciliation or an explicit operator acknowledgement has
        established that no ambiguous order survived the crash.
        """
        return self._recovery_degraded

    @property
    def recovery_error(self) -> str:
        return self._recovery_error

    @property
    def quarantine_path(self) -> Path | None:
        return self._quarantine_path

    def _mark_unavailable(self, reason: str) -> None:
        if self._available:
            self._available = False
            logger.critical(
                "DECISION JOURNAL UNAVAILABLE (%s) — new risk-increasing "
                "orders will be rejected until this is resolved", reason,
            )

    def _recover_valid_prefix(self) -> None:
        """Quarantine a malformed JSONL segment while retaining its valid prefix.

        A process crash can leave the final write truncated even though every
        earlier record is durable.  Silently discarding the whole replay would
        reopen the duplicate-order window, so recovery is deliberately visible
        and leaves the journal in a fail-closed degraded state.
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        valid_lines: list[str] = []
        bad_line: int | None = None
        bad_reason = ""
        with self.path.open(encoding="utf-8") as handle:
            for lineno, raw in enumerate(handle, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                    if not isinstance(record, dict):
                        raise TypeError("record is not a JSON object")
                except (json.JSONDecodeError, TypeError) as exc:
                    bad_line = lineno
                    bad_reason = str(exc)
                    break
                valid_lines.append(stripped)
        if bad_line is None:
            return

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = self.path.with_name(f"{self.path.name}.corrupt.{stamp}")
        os.replace(self.path, quarantine)
        with self.path.open("w", encoding="utf-8") as handle:
            for line in valid_lines:
                handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._recovery_degraded = True
        self._recovery_error = f"malformed record at line {bad_line}: {bad_reason}"
        self._quarantine_path = quarantine
        logger.critical(
            "DECISION JOURNAL RECOVERY DEGRADED (%s); original quarantined at %s — "
            "new entries remain blocked until reconciliation/operator acknowledgement",
            self._recovery_error,
            quarantine,
        )

    def acknowledge_recovery(self, note: str) -> bool:
        """Explicitly clear a degraded recovery latch after operator reconciliation."""
        if not self._recovery_degraded:
            return True
        note = str(note).strip()
        if not note:
            raise ValueError("recovery acknowledgement requires an operator note")
        if not self.append(
            "journal_recovery_acknowledged",
            {
                "note": note,
                "quarantine_path": str(self._quarantine_path or ""),
                "recovery_error": self._recovery_error,
            },
        ):
            return False
        self._recovery_degraded = False
        return True

    def append(self, kind: str, payload: dict[str, Any]) -> bool:
        """Write one record. Returns False (and flips unavailable) on failure —
        never raises into the order path."""
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "payload": payload,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError as exc:
            self._mark_unavailable(str(exc))
            return False

    def read_all(self) -> list[dict]:
        """Full replay of the clean journal segment (recovery/tests/audit)."""
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records
