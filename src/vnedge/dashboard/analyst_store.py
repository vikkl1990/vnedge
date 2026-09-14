"""Append-only Analyst evidence, separate from candles, journals and ML labels.

Only the opt-in public Analyst worker writes here. Dashboard connections are
SQLite read-only. IDs identify content, not scanner decisions or permissions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_RECORD_BYTES = 4_000_000
MAX_DATABASE_BYTES = 1_000_000_000


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
        total = sum(
            p.stat().st_size for p in (self.path, Path(str(self.path) + "-wal")) if p.exists()
        )
        if total > MAX_DATABASE_BYTES:
            raise ValueError("evidence_storage_full_archive_required")
        record_id = digest([kind, scope, body])
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO evidence VALUES (?, ?, ?, ?, ?)",
                (record_id, kind, scope, utc(now).isoformat(), zlib.compress(raw)),
            )
        return record_id

    def read(self, kind: str, scope: str, *, now: datetime, limit: int = 1) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("evidence_read_bound")
        if not self.path.exists():
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, body, available_at FROM evidence WHERE kind=? AND scope=? "
                "AND available_at<=? ORDER BY available_at DESC, rowid DESC LIMIT ?",
                (kind, scope, utc(now).isoformat(), limit),
            ).fetchall()
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
