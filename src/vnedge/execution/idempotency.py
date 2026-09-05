"""Idempotency: intent keys and client order ids.

Two distinct identities, per docs/DESIGN.md §2:

- **intent_key** — the deterministic identity of a trading DECISION
  (strategy, symbol, side, decision-bar timestamp). If the same decision is
  presented twice — a replayed signal, a crash-recovery re-run — the second
  presentation is a duplicate and is dropped loudly.
- **client_order_id** — the idempotency key the VENUE sees. Minted exactly
  once per venue attempt (uuid-based, no timestamp derivation), persisted
  before submission, and reused verbatim while that attempt is ambiguous.
  A definitive rejection ends the attempt; a permitted reduce-only
  resubmission gets a new id while retaining the same decision identity.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC

import pandas as pd


def make_decision_id(
    *,
    strategy_id: str,
    strategy_version: str,
    symbol: str,
    timeframe: str,
    decision_bar_content_hash: str,
    side: str,
    snapshot_id: str,
    entry_clock: str,
) -> str:
    """Hash closed-bar ARM truth; never use it as a venue id.

    Quote observations, the kernel path and the random venue id deliberately
    do not participate.  They are later events on this decision stream.
    """

    if len(decision_bar_content_hash) != 64 or any(
        char not in "0123456789abcdef"
        for char in decision_bar_content_hash.lower()
    ):
        raise ValueError("decision_bar_content_hash must be a SHA-256 hex digest")
    if len(snapshot_id) != 24 or any(
        char not in "0123456789abcdef" for char in snapshot_id.lower()
    ):
        raise ValueError("snapshot_id must be a 24-character hex digest")
    if not entry_clock.strip():
        raise ValueError("entry_clock is required")
    payload = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": symbol,
        "timeframe": timeframe,
        "decision_bar_content_hash": decision_bar_content_hash.lower(),
        "side": side,
        "snapshot_id": snapshot_id,
        "entry_clock": entry_clock,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"dec_{hashlib.sha256(encoded).hexdigest()[:24]}"


def make_intent_key(
    strategy_id: str,
    symbol: str,
    side: str,
    decision_bar_ts: pd.Timestamp,
    *,
    timeframe: str = "unreported",
    version: str = "unversioned",
    snapshot_id: str | None = None,
) -> str:
    """Legacy deterministic key for pre-envelope callers.

    Operational entry paths must use ``DecisionEnvelope.decision_id``.  This
    adapter remains only for journal recovery/tests that predate content-bound
    decision identities.
    """

    stamp = decision_bar_ts.to_pydatetime()
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("decision bar timestamp must be timezone-aware")
    payload = {
        "strategy_id": strategy_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "bar_open": stamp.astimezone(UTC).isoformat(),
        "side": side,
        "version": version,
        "snapshot_id": snapshot_id,
        "identity_contract": "legacy_bar_open_v1",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"dec_legacy_{hashlib.sha256(encoded).hexdigest()[:24]}"


def mint_client_order_id(prefix: str = "vne") -> str:
    """Random, minted ONCE per venue attempt. Never derived from time or signal
    values — collision-by-coincidence and divergence-on-retry are both
    failure modes we refuse."""
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class IntentRegistry:
    """In-memory duplicate-intent guard. The journal is the durable record;
    this is the fast in-session gate."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def register(self, intent_key: str, client_order_id: str) -> bool:
        """True if newly registered; False if this decision was already seen."""
        if intent_key in self._seen:
            return False
        self._seen[intent_key] = client_order_id
        return True

    def replace_terminal_attempt(
        self,
        intent_key: str,
        *,
        previous_client_order_id: str,
        client_order_id: str,
    ) -> bool:
        """Point one decision at a new, explicitly resubmitted attempt.

        This is only for a caller that already proved the previous venue
        attempt terminal.  Ambiguous transport retries must stay inside the
        adapter and reuse the previous client id instead.
        """

        if self._seen.get(intent_key) != previous_client_order_id:
            return False
        self._seen[intent_key] = client_order_id
        return True

    def restore(self, intent_key: str, client_order_id: str) -> None:
        """Restore the latest durable attempt for a decision during replay."""

        self._seen[intent_key] = client_order_id

    def existing_order_id(self, intent_key: str) -> str | None:
        return self._seen.get(intent_key)
