"""Exchange-credential settings domain and service.

Credentials are measurement-neutral: storing or verifying one never mutates a
runtime mode, a promotion record, a kill switch, or ``live_trading_enabled``.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import ClassVar, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vnedge.execution.operator_audit import OperatorAuditLog
from vnedge.settings.crypto import SecretBox
from vnedge.settings.profile import OperatorProfile
from vnedge.settings.store import SettingsStore, StoredExchangeConnection


class ExchangeId(str, Enum):
    BINANCEUSDM = "binanceusdm"
    BYBIT = "bybit"
    DELTA_INDIA = "delta_india"


class ConnectionStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"
    VERIFIED = "verified"
    INVALID = "invalid"
    DISABLED = "disabled"


class KeyPurpose(str, Enum):
    READ = "read"
    TRADE = "trade"


@dataclass(frozen=True, slots=True)
class ExchangeConnectionPublic:
    exchange: ExchangeId
    purpose: KeyPurpose
    status: ConnectionStatus
    api_key_hint: str
    permissions_note: str
    last_verified_at: datetime | None
    last_error: str | None
    can_trade: bool
    private_stream: str

    def public_dict(self) -> dict:
        payload = asdict(self)
        payload["exchange"] = self.exchange.value
        payload["purpose"] = self.purpose.value
        payload["status"] = self.status.value
        payload["last_verified_at"] = (
            self.last_verified_at.isoformat() if self.last_verified_at else None
        )
        return payload


@dataclass(frozen=True, slots=True)
class VerificationResult:
    ok: bool
    latency_ms: int
    permissions_note: str = "unknown"
    private_stream: str = "not_implemented"
    error: str | None = None


class ConnectionVerifier(Protocol):
    async def verify(self, exchange: ExchangeId, api_key: str, api_secret: str) -> VerificationResult:
        """Perform an authenticated read-only call. Implementations must never order."""


class CcxtReadOnlyVerifier:
    """Verify credentials with CCXT ``fetch_balance`` only; never places orders."""

    _CCXT_IDS: ClassVar[dict[ExchangeId, str]] = {ExchangeId.DELTA_INDIA: "delta"}

    async def verify(self, exchange: ExchangeId, api_key: str, api_secret: str) -> VerificationResult:
        started = time.monotonic()

        def _probe() -> None:
            import ccxt  # imported only on an explicit operator test

            class_name = self._CCXT_IDS.get(exchange, exchange.value)
            exchange_cls = getattr(ccxt, class_name)
            client = exchange_cls(
                {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
            )
            try:
                client.fetch_balance()
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

        try:
            await asyncio.to_thread(_probe)
        except Exception as exc:  # noqa: BLE001 — external venue boundary
            return VerificationResult(
                ok=False,
                latency_ms=round((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: authenticated read check failed",
            )
        return VerificationResult(
            ok=True,
            latency_ms=round((time.monotonic() - started) * 1000),
            permissions_note="unknown",
            private_stream="not_implemented" if exchange is ExchangeId.DELTA_INDIA else "not_required",
        )


class TestRateLimitError(RuntimeError):
    pass


class SettingsService:
    """Validated settings operations with audit and fail-closed credential loads."""

    def __init__(
        self,
        store: SettingsStore,
        secret_box: SecretBox | None,
        audit_log: OperatorAuditLog,
        verifier: ConnectionVerifier | None = None,
    ) -> None:
        self.store = store
        self.secret_box = secret_box
        self.audit_log = audit_log
        self.verifier = verifier or CcxtReadOnlyVerifier()
        self._test_attempts: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    @property
    def secrets_ready(self) -> bool:
        return self.secret_box is not None

    def profile(self, operator_id: str) -> OperatorProfile:
        return self.store.get_or_create_profile(operator_id)

    def update_profile(self, operator_id: str, display_name: str, timezone: str) -> OperatorProfile:
        clean_name = display_name.strip()
        if not clean_name or len(clean_name) > 80:
            raise ValueError("display_name must contain 1-80 characters")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        before = self.profile(operator_id).public_dict()
        profile = self.store.update_profile(operator_id, clean_name, timezone)
        self.audit_log.record(
            actor=operator_id,
            action="settings.profile.update",
            source="dashboard_settings",
            before=before,
            after=profile.public_dict(),
        )
        return profile

    @staticmethod
    def _empty(exchange: ExchangeId) -> ExchangeConnectionPublic:
        return ExchangeConnectionPublic(
            exchange=exchange,
            purpose=KeyPurpose.READ,
            status=ConnectionStatus.NOT_CONFIGURED,
            api_key_hint="",
            permissions_note="unknown",
            last_verified_at=None,
            last_error=None,
            can_trade=False,
            private_stream="not_implemented" if exchange is ExchangeId.DELTA_INDIA else "not_required",
        )

    @staticmethod
    def _public(row: StoredExchangeConnection) -> ExchangeConnectionPublic:
        status = ConnectionStatus(row.status)
        purpose = KeyPurpose(row.purpose)
        return ExchangeConnectionPublic(
            exchange=ExchangeId(row.exchange),
            purpose=purpose,
            status=status,
            api_key_hint=row.api_key_hint,
            permissions_note=row.permissions_note,
            last_verified_at=row.last_verified_at,
            last_error=row.last_error,
            can_trade=(
                purpose is KeyPurpose.TRADE
                and status is ConnectionStatus.VERIFIED
                and not row.disabled
            ),
            private_stream=(
                "not_implemented" if row.exchange == ExchangeId.DELTA_INDIA.value else "not_required"
            ),
        )

    def list_connections(self) -> list[ExchangeConnectionPublic]:
        configured = {row.exchange: row for row in self.store.list_connections()}
        return [
            self._public(configured[exchange.value])
            if exchange.value in configured
            else self._empty(exchange)
            for exchange in ExchangeId
        ]

    def connection(self, exchange: ExchangeId) -> ExchangeConnectionPublic:
        row = self.store.get_connection(exchange.value)
        return self._public(row) if row else self._empty(exchange)

    def upsert(
        self,
        exchange: ExchangeId,
        purpose: KeyPurpose,
        api_key: str,
        api_secret: str,
        operator_id: str,
        *,
        withdrawal_disabled_ack: bool,
    ) -> ExchangeConnectionPublic:
        if self.secret_box is None:
            raise RuntimeError("credential encryption is unavailable; set VNEDGE_SECRETS_KEY")
        key = api_key.strip()
        secret = api_secret.strip()
        if not key or not secret:
            raise ValueError("api_key and api_secret are both required")
        if len(key) > 512 or len(secret) > 512:
            raise ValueError("credential fields exceed the maximum length")
        if purpose is KeyPurpose.TRADE and not withdrawal_disabled_ack:
            raise ValueError("confirm that withdrawal permission is disabled")
        now = datetime.now(UTC)
        row = StoredExchangeConnection(
            exchange=exchange.value,
            purpose=purpose.value,
            status=ConnectionStatus.CONFIGURED.value,
            api_key_hint=f"••••{key[-4:]}",
            api_key_encrypted=self.secret_box.seal(key),
            api_secret_encrypted=self.secret_box.seal(secret),
            key_version=self.secret_box.key_version,
            permissions_note=purpose.value,
            last_verified_at=None,
            last_error=None,
            disabled=False,
            updated_at=now,
        )
        previous = self.store.get_connection(exchange.value)
        self.store.save_connection(row)
        self.audit_log.record(
            actor=operator_id,
            action="settings.keys.rotate" if previous else "settings.keys.upsert",
            detail=f"exchange={exchange.value} purpose={purpose.value}",
            source="dashboard_settings",
            before={"status": previous.status, "purpose": previous.purpose} if previous else None,
            after={"status": row.status, "purpose": row.purpose},
        )
        return self._public(row)

    def load_credentials(
        self, exchange: ExchangeId, *, require_trade: bool = False
    ) -> tuple[str, str] | None:
        """Execution-process seam. Disabled/unverified/mismatched rows never load."""
        row = self.store.get_connection(exchange.value)
        if (
            row is None
            or row.disabled
            or row.status != ConnectionStatus.VERIFIED.value
            or (require_trade and row.purpose != KeyPurpose.TRADE.value)
            or self.secret_box is None
            or row.key_version != self.secret_box.key_version
        ):
            return None
        return (
            self.secret_box.open(row.api_key_encrypted),
            self.secret_box.open(row.api_secret_encrypted),
        )

    def _check_rate_limit(self, operator_id: str, exchange: ExchangeId) -> None:
        now = time.monotonic()
        attempts = self._test_attempts[(operator_id, exchange.value)]
        while attempts and now - attempts[0] >= 60:
            attempts.popleft()
        if len(attempts) >= 5:
            raise TestRateLimitError("connection tests are limited to 5 per minute")
        attempts.append(now)

    async def test_connection(
        self, exchange: ExchangeId, operator_id: str
    ) -> tuple[ExchangeConnectionPublic, int]:
        self._check_rate_limit(operator_id, exchange)
        row = self.store.get_connection(exchange.value)
        if row is None or row.disabled:
            raise ValueError("connection is not configured or is disabled")
        if self.secret_box is None or row.key_version != self.secret_box.key_version:
            raise RuntimeError("credential encryption key is unavailable or has changed")
        key = self.secret_box.open(row.api_key_encrypted)
        secret = self.secret_box.open(row.api_secret_encrypted)
        result = await self.verifier.verify(exchange, key, secret)
        status = ConnectionStatus.VERIFIED if result.ok else ConnectionStatus.INVALID
        updated = self.store.update_status(
            exchange.value,
            status=status.value,
            last_error=None if result.ok else (result.error or "authentication check failed"),
            last_verified_at=datetime.now(UTC) if result.ok else None,
            disabled=False,
        )
        assert updated is not None
        self.audit_log.record(
            actor=operator_id,
            action="settings.connection.test",
            detail=f"exchange={exchange.value} result={status.value} latency_ms={result.latency_ms}",
            source="dashboard_settings",
            before={"status": row.status},
            after={"status": status.value},
        )
        return self._public(updated), result.latency_ms

    def disable(self, exchange: ExchangeId, operator_id: str) -> ExchangeConnectionPublic:
        row = self.store.get_connection(exchange.value)
        if row is None:
            raise ValueError("connection is not configured")
        updated = self.store.update_status(
            exchange.value,
            status=ConnectionStatus.DISABLED.value,
            last_error=None,
            last_verified_at=row.last_verified_at,
            disabled=True,
        )
        assert updated is not None
        self.audit_log.record(
            actor=operator_id,
            action="settings.connection.disable",
            detail=f"exchange={exchange.value}",
            source="dashboard_settings",
            before={"status": row.status},
            after={"status": ConnectionStatus.DISABLED.value},
        )
        return self._public(updated)

    def delete(self, exchange: ExchangeId, operator_id: str) -> bool:
        existed = self.store.delete_connection(exchange.value)
        if existed:
            self.audit_log.record(
                actor=operator_id,
                action="settings.connection.delete",
                detail=f"exchange={exchange.value}; encrypted material removed",
                source="dashboard_settings",
            )
        return existed
