"""Per-user dashboard auth: named bearer tokens with roles and expiry.

Replaces the single shared ``DASHBOARD_TOKEN`` with a token *store*:

- ``DASHBOARD_USERS`` env: ``name:token:role[:expiry_iso]`` entries joined
  by ``;`` (the expiry field may itself contain ``:``  — ISO-8601 datetimes
  do — so it is always parsed as "everything after the third colon").
- ``DASHBOARD_TOKEN`` env (back-compat): still accepted, as the
  ``operator`` user with no expiry, so existing deploys keep working
  without any env change.

Roles are ``viewer`` and ``operator``. Both are read-only today — the
dashboard has zero control routes — the role exists so any future
privileged surface can distinguish them without another auth migration.

Security invariants:
- token comparison is constant-time per stored token, and every stored
  token is compared on every attempt (no early exit on match), so timing
  does not reveal which entry matched;
- token values are never logged and never echoed in responses; auth events
  carry the user name and role only;
- malformed ``DASHBOARD_USERS`` entries are skipped LOUDLY (warning log,
  token text withheld) rather than silently ignored;
- expired tokens are rejected with an explicit reason, never treated as
  merely unknown.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ROLES: tuple[str, ...] = ("viewer", "operator")
#: Identity assigned to the legacy shared DASHBOARD_TOKEN.
LEGACY_USER_NAME = "operator"


@dataclass(frozen=True)
class DashboardUser:
    """One authorized dashboard identity. ``token`` is a bearer secret:
    it must never be logged or serialized into any response."""

    name: str
    token: str
    role: str  # "viewer" | "operator"
    expires_at: datetime | None = None  # None = no expiry (tz-aware otherwise)


@dataclass(frozen=True)
class AuthResult:
    """Outcome of one authentication attempt. Carries identity (never the
    token) so routes can attach ``X-Dashboard-User`` and log auth events."""

    authorized: bool
    name: str | None = None
    role: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None  # populated on rejection, safe to echo in a 401


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_users_env(raw: str) -> list[DashboardUser]:
    """Parse ``DASHBOARD_USERS`` (``name:token:role[:expiry_iso];...``).

    Defensive by design: a malformed entry never takes the dashboard down
    and never poisons its neighbours — it is skipped with a WARNING that
    names the entry position (and user name when parseable) but never the
    token text.
    """
    users: list[DashboardUser] = []
    seen_names: set[str] = set()
    for idx, chunk in enumerate(raw.split(";")):
        entry = chunk.strip()
        if not entry:
            continue
        parts = entry.split(":", 3)  # expiry keeps its own colons intact
        if len(parts) < 3:
            logger.warning(
                "DASHBOARD_USERS entry %d skipped: expected name:token:role[:expiry_iso]", idx
            )
            continue
        name = parts[0].strip()
        token = parts[1].strip()
        role = parts[2].strip().lower()
        expiry_raw = parts[3].strip() if len(parts) == 4 else ""
        if not name or not token:
            logger.warning("DASHBOARD_USERS entry %d skipped: empty name or token", idx)
            continue
        if role not in ROLES:
            logger.warning(
                "DASHBOARD_USERS entry %d (%r) skipped: unknown role %r (expected %s)",
                idx, name, role, "|".join(ROLES),
            )
            continue
        expires_at: datetime | None = None
        if expiry_raw:
            try:
                expires_at = datetime.fromisoformat(expiry_raw)
            except ValueError:
                logger.warning(
                    "DASHBOARD_USERS entry %d (%r) skipped: unparseable expiry %r "
                    "(expected ISO-8601, e.g. 2026-08-01T00:00:00+00:00)",
                    idx, name, expiry_raw,
                )
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)  # naive = UTC
        if name in seen_names:
            logger.warning(
                "DASHBOARD_USERS entry %d skipped: duplicate user name %r", idx, name
            )
            continue
        seen_names.add(name)
        users.append(DashboardUser(name=name, token=token, role=role, expires_at=expires_at))
    return users


class TokenStore:
    """Immutable set of authorized dashboard users.

    ``authenticate`` is the only way in: it compares the candidate against
    EVERY stored token with :func:`hmac.compare_digest` (constant-time per
    token, no early exit) and enforces expiry on the matched entry.
    """

    def __init__(self, users: Sequence[DashboardUser] = ()) -> None:
        self._users: tuple[DashboardUser, ...] = tuple(users)

    def __len__(self) -> int:
        return len(self._users)

    @property
    def users(self) -> tuple[DashboardUser, ...]:
        return self._users

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TokenStore:
        """Load ``DASHBOARD_USERS`` plus the back-compat ``DASHBOARD_TOKEN``
        (mapped to the ``operator`` user, role=operator, no expiry)."""
        source = os.environ if env is None else env
        users = parse_users_env(source.get("DASHBOARD_USERS", ""))
        legacy = (source.get("DASHBOARD_TOKEN") or "").strip()
        if legacy:
            users.append(
                DashboardUser(name=LEGACY_USER_NAME, token=legacy, role="operator")
            )
        return cls(users)

    def authenticate(self, candidate: str, now: datetime | None = None) -> AuthResult:
        moment = now if now is not None else _utcnow()
        candidate_bytes = (candidate or "").encode("utf-8")
        matched: DashboardUser | None = None
        for user in self._users:
            # Compare every token; keep the first match without breaking out
            # so the loop's timing is independent of match position.
            if hmac.compare_digest(candidate_bytes, user.token.encode("utf-8")):
                if matched is None:
                    matched = user
        if matched is None:
            return AuthResult(authorized=False, reason="missing or invalid token")
        if matched.expires_at is not None and moment >= matched.expires_at:
            logger.warning(
                "dashboard auth rejected: user=%s role=%s token expired at %s",
                matched.name, matched.role, matched.expires_at.isoformat(),
            )
            return AuthResult(
                authorized=False,
                name=matched.name,
                role=matched.role,
                expires_at=matched.expires_at,
                reason=f"token expired at {matched.expires_at.isoformat()}",
            )
        logger.info("dashboard auth accepted: user=%s role=%s", matched.name, matched.role)
        return AuthResult(
            authorized=True,
            name=matched.name,
            role=matched.role,
            expires_at=matched.expires_at,
        )
