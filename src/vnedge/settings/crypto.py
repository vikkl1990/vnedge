"""Authenticated encryption for server-only settings secrets.

The wrapping key is supplied only through ``VNEDGE_SECRETS_KEY``.  There is no
generated fallback: losing an ephemeral key would make persisted credentials
undecryptable after a restart, so a missing key deliberately disables secret
writes and adapter credential loading.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from cryptography.fernet import Fernet, InvalidToken


class SecretBoxError(RuntimeError):
    """A sealed value cannot be safely opened."""


class SecretBox:
    """Small Fernet wrapper with an explicit key version for future rotation."""

    def __init__(self, key: str | bytes, *, key_version: int = 1) -> None:
        encoded = key.encode("ascii") if isinstance(key, str) else key
        try:
            self._fernet = Fernet(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError("VNEDGE_SECRETS_KEY must be a valid Fernet key") from exc
        if key_version < 1:
            raise ValueError("key_version must be positive")
        self.key_version = key_version

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SecretBox | None:
        source = os.environ if env is None else env
        key = (source.get("VNEDGE_SECRETS_KEY") or "").strip()
        if not key:
            return None
        raw_version = (source.get("VNEDGE_SECRETS_KEY_VERSION") or "1").strip()
        try:
            version = int(raw_version)
        except ValueError as exc:
            raise ValueError("VNEDGE_SECRETS_KEY_VERSION must be an integer") from exc
        return cls(key, key_version=version)

    def seal(self, plaintext: str) -> bytes:
        if not plaintext:
            raise ValueError("refusing to encrypt an empty secret")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def open(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise SecretBoxError("sealed credential could not be decrypted") from exc
