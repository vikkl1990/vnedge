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

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JOURNAL_SCHEMA_VERSION = 2
_GENESIS_HASH = "0" * 64


def _journal_record_hash(record: dict[str, Any], prev_hash: str) -> str:
    """Hash one immutable WAL body against the preceding record."""

    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{prev_hash}|{canonical}".encode()).hexdigest()


def _v2_record_error(record: dict[str, Any]) -> str | None:
    """Validate one v2 record without walking its preceding chain."""

    if record.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        return f"unsupported schema_version {record.get('schema_version')!r}"
    seq = record.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return "invalid journal seq"
    prev_hash = record.get("prev_hash")
    claimed = record.get("hash")
    if not isinstance(prev_hash, str) or len(prev_hash) != 64:
        return "invalid prev_hash"
    if not isinstance(claimed, str) or len(claimed) != 64:
        return "invalid hash"
    core = {key: value for key, value in record.items() if key not in {"prev_hash", "hash"}}
    if _journal_record_hash(core, prev_hash) != claimed:
        return "journal record hash mismatch"
    return None


@dataclass(frozen=True)
class JournalChainReport:
    ok: bool
    records: int
    chained_records: int
    legacy_records: int
    first_bad_line: int | None = None
    reason: str = ""


def verify_journal_chain(path: Path | str) -> JournalChainReport:
    """Offline full-file validation.

    Schema-1 rows are accepted only as a legacy prefix.  The first schema-2
    row starts a new chain at sequence zero; legacy rows may never appear
    after that boundary.
    """

    path = Path(path)
    if not path.exists():
        return JournalChainReport(True, 0, 0, 0)
    prev_hash = _GENESIS_HASH
    expected_seq = 0
    records = chained = legacy = 0
    chain_started = False
    with path.open(encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                return JournalChainReport(
                    False, records, chained, legacy, lineno, f"invalid JSON: {exc}"
                )
            if not isinstance(record, dict):
                return JournalChainReport(
                    False, records, chained, legacy, lineno, "record is not an object"
                )
            schema = record.get("schema_version")
            if schema is None:
                if chain_started:
                    return JournalChainReport(
                        False,
                        records,
                        chained,
                        legacy,
                        lineno,
                        "legacy row after schema-2 chain start",
                    )
                legacy += 1
                records += 1
                continue
            chain_started = True
            error = _v2_record_error(record)
            if error is None and record.get("seq") != expected_seq:
                error = f"expected seq {expected_seq}, got {record.get('seq')}"
            if error is None and record.get("prev_hash") != prev_hash:
                error = "previous hash link mismatch"
            if error is not None:
                return JournalChainReport(
                    False, records, chained, legacy, lineno, error
                )
            prev_hash = str(record["hash"])
            expected_seq += 1
            chained += 1
            records += 1
    return JournalChainReport(True, records, chained, legacy)

class DecisionJournal:
    def __init__(self, path: Path | str, *, path_id: str | None = None) -> None:
        self.path = Path(path)
        self.path_id = str(path_id).strip() if path_id is not None else None
        if path_id is not None and not self.path_id:
            raise ValueError("path_id must be non-empty when provided")
        self._available = True
        self._recovery_degraded = False
        self._recovery_error = ""
        self._quarantine_path: Path | None = None
        self._next_seq = 0
        self._prev_hash = _GENESIS_HASH
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Probe writability at startup, not at first order.
            with open(self.path, "a", encoding="utf-8"):
                pass
            self._recover_valid_prefix()
            self._resume_chain_from_tail()
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
        # append() performs one line-buffered, flushed+fsynced write. A crash
        # can therefore tear only the final record. Re-reading and retaining
        # every historical line on each process start made recovery O(file)
        # in time *and* memory; high-volume evidence journals reached ~1 GiB
        # and held the scanner process unavailable for minutes. Validate the
        # crash boundary only. Full historical validation belongs to the
        # offline evidence audit, while the hot WAL recovery remains strict
        # about the only segment the current process could have torn.
        size = self.path.stat().st_size
        tail_bytes = min(size, 1 << 20)
        with self.path.open("rb") as handle:
            handle.seek(size - tail_bytes)
            tail = handle.read(tail_bytes)
        stripped = tail.rstrip(b"\r\n\t ")
        if not stripped:
            return
        line_start = stripped.rfind(b"\n") + 1
        if line_start == 0 and tail_bytes < size:
            # A journal record larger than 1 MiB violates the runtime record
            # contract. Refuse it rather than guessing where its prefix began.
            bad_offset = size - tail_bytes
            bad_reason = "final journal record exceeds 1 MiB"
        else:
            bad_offset = size - tail_bytes + line_start
            raw_line = stripped[line_start:]
            try:
                record = json.loads(raw_line.decode("utf-8"))
                if not isinstance(record, dict):
                    raise TypeError("record is not a JSON object")
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
                bad_reason = str(exc)
            else:
                if record.get("schema_version") is not None:
                    integrity_error = _v2_record_error(record)
                    if integrity_error is not None:
                        bad_reason = integrity_error
                    else:
                        integrity_error = None
                else:
                    integrity_error = None
                if integrity_error is not None:
                    # A syntactically complete but modified final record is
                    # still a torn/untrusted WAL boundary.
                    pass
                else:
                    # A complete JSON record without its newline would otherwise
                    # concatenate with the next append. Repair that delimiter and
                    # keep the journal available; no decision body was lost.
                    if not tail.endswith(b"\n"):
                        with self.path.open("ab") as handle:
                            handle.write(b"\n")
                            handle.flush()
                            os.fsync(handle.fileno())
                    return

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = self.path.with_name(f"{self.path.name}.corrupt.{stamp}")
        os.replace(self.path, quarantine)
        with quarantine.open("rb") as source, self.path.open("wb") as target:
            remaining = bad_offset
            while remaining > 0:
                chunk = source.read(min(1 << 20, remaining))
                if not chunk:
                    break
                target.write(chunk)
                remaining -= len(chunk)
            target.flush()
            os.fsync(target.fileno())
        self._recovery_degraded = True
        self._recovery_error = f"malformed final record at byte {bad_offset}: {bad_reason}"
        self._quarantine_path = quarantine
        logger.critical(
            "DECISION JOURNAL RECOVERY DEGRADED (%s); original quarantined at %s — "
            "new entries remain blocked until reconciliation/operator acknowledgement",
            self._recovery_error,
            quarantine,
        )

    def _resume_chain_from_tail(self) -> None:
        """Resume the v2 chain from the last complete row without an O(file) scan."""

        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        size = self.path.stat().st_size
        tail_bytes = min(size, 1 << 20)
        with self.path.open("rb") as handle:
            handle.seek(size - tail_bytes)
            lines = handle.read(tail_bytes).decode("utf-8").splitlines()
        for raw in reversed(lines):
            if not raw.strip():
                continue
            record = json.loads(raw)
            if record.get("schema_version") == JOURNAL_SCHEMA_VERSION:
                self._next_seq = int(record["seq"]) + 1
                self._prev_hash = str(record["hash"])
            # A legacy final row starts the first schema-2 chain epoch. Full
            # offline validation will reject any legacy row inserted later.
            return

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
        body = dict(payload)
        if self.path_id is not None:
            supplied = body.get("path_id")
            if supplied is not None and supplied != self.path_id:
                self._mark_unavailable(
                    f"journal path_id conflict: expected {self.path_id}, got {supplied}"
                )
                return False
            # A journal file contains market/research and execution evidence.
            # Only records emitted by the execution spine may claim its path.
            # ShadowOutcomeTracker/scanner rows therefore remain explicitly
            # ineligible instead of inheriting kernel_v1 from the file.
            # The caller must prove that a record crossed the kernel.  Merely
            # naming an event ``order_*`` is not authority: compatibility and
            # research callers can still use OrderManager directly, and those
            # rows must remain ineligible for operational P&L.
            if supplied is not None:
                body["path_id"] = self.path_id
        core = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "seq": self._next_seq,
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "payload": body,
        }
        digest = _journal_record_hash(core, self._prev_hash)
        record = {
            **core,
            "prev_hash": self._prev_hash,
            "hash": digest,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._prev_hash = digest
            self._next_seq += 1
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
