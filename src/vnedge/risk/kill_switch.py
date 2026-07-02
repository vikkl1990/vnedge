"""Kill switch — the last line of defense.

Two independent trip mechanisms:

1. **Programmatic** — any component calls :meth:`KillSwitch.activate` with a
   reason (daily loss breached, reconciliation mismatch, stale data storm...).
2. **File-based** — an operator runs ``touch KILL`` in the working directory.
   This works even when the process is wedged enough that APIs don't respond,
   and requires no tooling beyond a shell.

Once tripped, the switch stays tripped until a human calls :meth:`reset` —
it never auto-resets. The pre-trade gateway rejects every order while active;
the live trader is responsible for flattening positions reduce-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class KillSwitchEvent:
    timestamp: datetime
    action: str  # "activate" | "reset"
    reason: str
    source: str  # "programmatic" | "file"


@dataclass
class KillSwitch:
    kill_file: Path = Path("KILL")
    _active: bool = False
    _reason: str = ""
    # Append-only in-memory audit trail; also mirrored to the log. The
    # persistent audit ledger subscribes to these events in a later milestone.
    history: list[KillSwitchEvent] = field(default_factory=list)

    def activate(self, reason: str, source: str = "programmatic") -> None:
        if self._active:
            return  # already tripped; first reason wins, do not overwrite
        self._active = True
        self._reason = reason
        event = KillSwitchEvent(datetime.now(UTC), "activate", reason, source)
        self.history.append(event)
        logger.critical("KILL SWITCH ACTIVATED (%s): %s", source, reason)

    def reset(self, operator_note: str) -> None:
        """Manual reset only. Requires a note for the audit trail."""
        if not operator_note.strip():
            raise ValueError("Kill switch reset requires an operator note for the audit trail")
        if self.kill_file.exists():
            raise RuntimeError(
                f"Kill file {self.kill_file} still exists — remove it explicitly before reset"
            )
        self._active = False
        self._reason = ""
        self.history.append(
            KillSwitchEvent(datetime.now(UTC), "reset", operator_note, "programmatic")
        )
        logger.warning("Kill switch reset by operator: %s", operator_note)

    @property
    def is_active(self) -> bool:
        """Check state, including the file trigger. Called before every order."""
        if not self._active and self.kill_file.exists():
            self.activate(f"kill file present: {self.kill_file}", source="file")
        return self._active

    @property
    def reason(self) -> str:
        return self._reason
