"""Encrypted operator Settings: redaction, RBAC/CSRF, audit, and runtime isolation."""

from __future__ import annotations

import json

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.auth import DashboardUser, TokenStore
from vnedge.dashboard.session import SessionIssuer
from vnedge.execution.operator_audit import OperatorAuditLog
from vnedge.settings.crypto import SecretBox
from vnedge.settings.exchange_connections import (
    ExchangeId,
    KeyPurpose,
    SettingsService,
    VerificationResult,
)
from vnedge.settings.store import SettingsStore


class ReadOnlyVerifier:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[ExchangeId, str, str]] = []
        self.orders: list[object] = []

    async def verify(
        self, exchange: ExchangeId, api_key: str, api_secret: str
    ) -> VerificationResult:
        self.calls.append((exchange, api_key, api_secret))
        return VerificationResult(
            ok=self.ok,
            latency_ms=12,
            error=None if self.ok else "AuthenticationError: authenticated read check failed",
        )


def _service(tmp_path, verifier=None) -> SettingsService:
    return SettingsService(
        SettingsStore(tmp_path / "settings.sqlite"),
        SecretBox(Fernet.generate_key()),
        OperatorAuditLog(tmp_path / "settings_audit.jsonl"),
        verifier=verifier,
    )


def _client(tmp_path, service: SettingsService) -> TestClient:
    provider = SnapshotProvider()
    provider.publish(
        {
            "mode": "shadow",
            "live": {"blocked": True},
            "live_orders_enabled": False,
        }
    )
    store = TokenStore(
        [
            DashboardUser(name="viewer", token="viewer-root", role="viewer"),
            DashboardUser(name="operator-1", token="operator-root", role="operator"),
        ]
    )
    return TestClient(
        create_app(
            provider,
            token_store=store,
            session_issuer=SessionIssuer(b"test-session-secret"),
            session_cookie_secure=False,
            settings_service=service,
        )
    )


def _session(client: TestClient, token: str = "operator-root") -> str:
    response = client.post("/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    csrf = client.cookies.get("vnedge_csrf")
    assert csrf
    return csrf


def test_ciphertext_only_public_dto_and_disable_blocks_runtime_load(tmp_path):
    verifier = ReadOnlyVerifier()
    service = _service(tmp_path, verifier)
    item = service.upsert(
        ExchangeId.DELTA_INDIA,
        KeyPurpose.TRADE,
        "api-key-ABCD",
        "super-secret-value",
        "operator-1",
        withdrawal_disabled_ack=True,
    )
    assert item.api_key_hint == "••••ABCD"
    assert "secret" not in json.dumps(item.public_dict()).lower()
    database_bytes = (tmp_path / "settings.sqlite").read_bytes()
    assert b"api-key-ABCD" not in database_bytes
    assert b"super-secret-value" not in database_bytes
    assert service.load_credentials(ExchangeId.DELTA_INDIA, require_trade=True) is None

    verified, latency = __import__("asyncio").run(
        service.test_connection(ExchangeId.DELTA_INDIA, "operator-1")
    )
    assert verified.status.value == "verified" and latency == 12
    assert verifier.calls == [
        (ExchangeId.DELTA_INDIA, "api-key-ABCD", "super-secret-value")
    ]
    assert verifier.orders == []
    assert service.load_credentials(ExchangeId.DELTA_INDIA, require_trade=True) == (
        "api-key-ABCD",
        "super-secret-value",
    )

    service.disable(ExchangeId.DELTA_INDIA, "operator-1")
    assert service.load_credentials(ExchangeId.DELTA_INDIA, require_trade=True) is None
    audit_text = (tmp_path / "settings_audit.jsonl").read_text()
    assert "settings.keys.upsert" in audit_text
    assert "settings.connection.test" in audit_text
    assert "settings.connection.disable" in audit_text
    assert "api-key-ABCD" not in audit_text and "super-secret-value" not in audit_text


def test_operator_cookie_session_requires_csrf_and_never_echoes_secrets(tmp_path):
    service = _service(tmp_path, ReadOnlyVerifier())
    client = _client(tmp_path, service)
    csrf = _session(client)

    public = client.get("/api/settings/exchanges")
    assert public.status_code == 200
    assert "api_secret" not in public.text and "api_key_encrypted" not in public.text

    payload = {
        "api_key": "visible-once-key",
        "api_secret": "visible-once-secret",
        "purpose": "trade",
        "withdrawal_disabled_ack": True,
    }
    refused = client.put("/api/settings/exchanges/bybit", json=payload)
    assert refused.status_code == 403

    saved = client.put(
        "/api/settings/exchanges/bybit",
        json=payload,
        headers={"X-VNEDGE-CSRF": csrf},
    )
    assert saved.status_code == 200
    assert saved.json()["api_key_hint"] == "••••-key"
    assert "visible-once" not in saved.text
    listing = client.get("/api/settings/exchanges").text
    assert "visible-once-key" not in listing and "visible-once-secret" not in listing

    snapshot = client.get("/state").text
    assert "visible-once-key" not in snapshot and "visible-once-secret" not in snapshot


def test_viewer_cannot_open_settings_and_profile_update_is_audited(tmp_path):
    service = _service(tmp_path)
    client = _client(tmp_path, service)
    _session(client, "viewer-root")
    assert client.get("/api/settings/profile").status_code == 403

    client.cookies.clear()
    csrf = _session(client)
    updated = client.put(
        "/api/settings/profile",
        json={"display_name": "Primary operator", "timezone": "Asia/Kolkata"},
        headers={"X-VNEDGE-CSRF": csrf},
    )
    assert updated.status_code == 200
    assert updated.json()["operator_id"] == "operator-1"
    assert "settings.profile.update" in (tmp_path / "settings_audit.jsonl").read_text()


def test_missing_encryption_key_fails_closed(tmp_path):
    service = SettingsService(
        SettingsStore(tmp_path / "settings.sqlite"),
        None,
        OperatorAuditLog(tmp_path / "audit.jsonl"),
    )
    client = _client(tmp_path, service)
    csrf = _session(client)
    status = client.get("/api/settings/security").json()
    assert status["secrets_store_ready"] is False
    response = client.put(
        "/api/settings/exchanges/binanceusdm",
        json={
            "api_key": "x",
            "api_secret": "y",
            "purpose": "read",
            "withdrawal_disabled_ack": False,
        },
        headers={"X-VNEDGE-CSRF": csrf},
    )
    assert response.status_code == 503
