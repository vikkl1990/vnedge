"""SQLite persistence for profiles and encrypted exchange connections.

Only ciphertext and a last-four display hint are stored.  The database never
contains a plaintext API key or secret.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vnedge.settings.profile import OperatorProfile


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True, slots=True)
class StoredExchangeConnection:
    exchange: str
    purpose: str
    status: str
    api_key_hint: str
    api_key_encrypted: bytes
    api_secret_encrypted: bytes
    key_version: int
    permissions_note: str
    last_verified_at: datetime | None
    last_error: str | None
    disabled: bool
    updated_at: datetime


class SettingsStore:
    """Transactional settings repository; one active connection per venue."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operator_profiles (
                    operator_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exchange_connections (
                    exchange TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL,
                    api_key_hint TEXT NOT NULL,
                    api_key_encrypted BLOB NOT NULL,
                    api_secret_encrypted BLOB NOT NULL,
                    key_version INTEGER NOT NULL,
                    permissions_note TEXT NOT NULL,
                    last_verified_at TEXT,
                    last_error TEXT,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def get_or_create_profile(self, operator_id: str) -> OperatorProfile:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operator_profiles WHERE operator_id = ?", (operator_id,)
            ).fetchone()
            if row is None:
                now = _now().isoformat()
                connection.execute(
                    "INSERT INTO operator_profiles VALUES (?, ?, ?, ?, ?)",
                    (operator_id, operator_id, "UTC", now, now),
                )
                row = connection.execute(
                    "SELECT * FROM operator_profiles WHERE operator_id = ?", (operator_id,)
                ).fetchone()
        assert row is not None
        return OperatorProfile(
            operator_id=row["operator_id"],
            display_name=row["display_name"],
            timezone=row["timezone"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def update_profile(self, operator_id: str, display_name: str, timezone: str) -> OperatorProfile:
        existing = self.get_or_create_profile(operator_id)
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """UPDATE operator_profiles
                   SET display_name = ?, timezone = ?, updated_at = ?
                   WHERE operator_id = ?""",
                (display_name, timezone, now.isoformat(), operator_id),
            )
        return OperatorProfile(
            operator_id=operator_id,
            display_name=display_name,
            timezone=timezone,
            created_at=existing.created_at,
            updated_at=now,
        )

    @staticmethod
    def _connection(row: sqlite3.Row | None) -> StoredExchangeConnection | None:
        if row is None:
            return None
        return StoredExchangeConnection(
            exchange=row["exchange"],
            purpose=row["purpose"],
            status=row["status"],
            api_key_hint=row["api_key_hint"],
            api_key_encrypted=bytes(row["api_key_encrypted"]),
            api_secret_encrypted=bytes(row["api_secret_encrypted"]),
            key_version=int(row["key_version"]),
            permissions_note=row["permissions_note"],
            last_verified_at=_parse_time(row["last_verified_at"]),
            last_error=row["last_error"],
            disabled=bool(row["disabled"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_connection(self, exchange: str) -> StoredExchangeConnection | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM exchange_connections WHERE exchange = ?", (exchange,)
            ).fetchone()
        return self._connection(row)

    def list_connections(self) -> list[StoredExchangeConnection]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM exchange_connections ORDER BY exchange"
            ).fetchall()
        return [item for row in rows if (item := self._connection(row)) is not None]

    def save_connection(self, row: StoredExchangeConnection) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO exchange_connections (
                       exchange, purpose, status, api_key_hint, api_key_encrypted,
                       api_secret_encrypted, key_version, permissions_note,
                       last_verified_at, last_error, disabled, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(exchange) DO UPDATE SET
                       purpose=excluded.purpose,
                       status=excluded.status,
                       api_key_hint=excluded.api_key_hint,
                       api_key_encrypted=excluded.api_key_encrypted,
                       api_secret_encrypted=excluded.api_secret_encrypted,
                       key_version=excluded.key_version,
                       permissions_note=excluded.permissions_note,
                       last_verified_at=excluded.last_verified_at,
                       last_error=excluded.last_error,
                       disabled=excluded.disabled,
                       updated_at=excluded.updated_at""",
                (
                    row.exchange,
                    row.purpose,
                    row.status,
                    row.api_key_hint,
                    row.api_key_encrypted,
                    row.api_secret_encrypted,
                    row.key_version,
                    row.permissions_note,
                    row.last_verified_at.isoformat() if row.last_verified_at else None,
                    row.last_error,
                    int(row.disabled),
                    row.updated_at.isoformat(),
                ),
            )

    def update_status(
        self,
        exchange: str,
        *,
        status: str,
        last_error: str | None,
        last_verified_at: datetime | None = None,
        disabled: bool | None = None,
    ) -> StoredExchangeConnection | None:
        row = self.get_connection(exchange)
        if row is None:
            return None
        replacement = StoredExchangeConnection(
            exchange=row.exchange,
            purpose=row.purpose,
            status=status,
            api_key_hint=row.api_key_hint,
            api_key_encrypted=row.api_key_encrypted,
            api_secret_encrypted=row.api_secret_encrypted,
            key_version=row.key_version,
            permissions_note=row.permissions_note,
            last_verified_at=last_verified_at,
            last_error=last_error,
            disabled=row.disabled if disabled is None else disabled,
            updated_at=_now(),
        )
        self.save_connection(replacement)
        return replacement

    def delete_connection(self, exchange: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM exchange_connections WHERE exchange = ?", (exchange,)
            )
        return cursor.rowcount > 0
