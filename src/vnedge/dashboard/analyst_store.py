"""Append-only Analyst evidence, separate from candles, journals and ML labels.

Only the opt-in public Analyst worker writes here. Dashboard connections are
SQLite read-only. IDs identify content, not scanner decisions or permissions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
import zlib
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_RECORD_BYTES = 4_000_000
MAX_DATABASE_BYTES = 1_000_000_000
MAX_SEGMENTS = 32
MIN_FREE_BYTES = 1_000_000_000


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def utc(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result.astimezone(UTC)


class AnalystStore:
    def __init__(self, path: Path, *, writable: bool = False, wal: bool = True) -> None:
        self.path = path
        self.writable = writable
        self.wal = wal
        if writable:
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection, connection:
                # Rollback journal supports a physically read-only dashboard
                # mount without creating WAL shared-memory sidecars. Existing
                # stores retain WAL by default; only the isolated history owner
                # opts out. SQLite locking still serializes writers/readers.
                connection.execute("PRAGMA journal_mode=" + ("WAL" if wal else "DELETE"))
                connection.execute("""CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
                    available_at TEXT NOT NULL, body BLOB NOT NULL)""")
                connection.execute("""CREATE INDEX IF NOT EXISTS evidence_scope
                    ON evidence(kind, scope, available_at DESC)""")

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise ValueError("evidence_symlink_refused")
        if self.writable:
            return sqlite3.connect(self.path, timeout=5)
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)

    def append(self, kind: str, scope: str, body: dict[str, Any], now: datetime) -> str:
        if not self.writable:
            raise ValueError("read_only_evidence_store")
        raw = encode(body)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("evidence_record_too_large")
        record_id = digest([kind, scope, body])
        paths = self._segments()
        # The worker holds the store's exclusive writer lease. Preserve full
        # segments in place; never prune evidence to make a health badge green.
        target = paths[-1]
        total = sum(p.stat().st_size for p in (target, Path(str(target) + "-wal")) if p.exists())
        if total + len(raw) > MAX_DATABASE_BYTES:
            if len(paths) >= MAX_SEGMENTS or shutil.disk_usage(self.path.parent).free < MIN_FREE_BYTES:
                raise ValueError("evidence_storage_full_archive_required")
            target = self.path.with_name(self.path.name + f".segment-{len(paths):06d}.sqlite")
            AnalystStore(target, writable=True, wal=self.wal)
        if target.is_symlink():
            raise ValueError("evidence_symlink_refused")
        with closing(sqlite3.connect(target, timeout=5)) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO evidence VALUES (?, ?, ?, ?, ?)",
                (record_id, kind, scope, utc(now).isoformat(), zlib.compress(raw)),
            )
        return record_id

    def _segments(self) -> list[Path]:
        paths = [self.path, *sorted(self.path.parent.glob(self.path.name + ".segment-*.sqlite"))]
        if len(paths) > MAX_SEGMENTS or any(path.is_symlink() for path in paths):
            raise ValueError("invalid_evidence_segments")
        return paths

    def read(self, kind: str, scope: str, *, now: datetime, limit: int = 1) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("evidence_read_bound")
        if not self.path.exists():
            return []
        candidates = []
        for index, path in enumerate(self._segments()):
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                for row in connection.execute(
                    "SELECT id, body, available_at, rowid FROM evidence WHERE kind=? AND scope=? "
                    "AND available_at<=? ORDER BY available_at DESC, rowid DESC LIMIT ?",
                    (kind, scope, utc(now).isoformat(), limit),
                ).fetchall():
                    candidates.append((*row, index))
        candidates.sort(key=lambda row: (row[2], row[4], row[3]), reverse=True)
        rows = []
        seen = set()
        for record_id, compressed, available, _, _ in candidates:
            if record_id not in seen:
                seen.add(record_id)
                rows.append((record_id, compressed, available))
            if len(rows) == limit:
                break
        results = []
        for record_id, compressed, available in rows:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed, MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES or not decoder.eof:
                raise ValueError("invalid_evidence_size")
            body = json.loads(raw)
            if digest([kind, scope, body]) != record_id:
                raise ValueError("evidence_hash_mismatch")
            results.append({"evidence_id": record_id, "available_at": available, "body": body})
        return results
