"""Immutable evidence envelope for one execution decision.

``OrderIntent`` is deliberately venue-facing and contains no strategy, quote,
or higher-timeframe provenance.  This envelope stays on the kernel/journal
side of the boundary and supplies the deterministic decision identity used by
replay and duplicate suppression.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vnedge.execution.idempotency import make_decision_id
from vnedge.runtime.execution_contract import KERNEL_PATH_ID
from vnedge.strategy.arm_evidence import (
    FrozenPermissionSnapshot,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution evidence timestamps must be timezone-aware")
    return value.astimezone(UTC)


def strategy_version_from_id(strategy_id: str) -> str:
    """Return the explicit ``vN`` suffix, or ``unversioned``."""

    head, marker, tail = strategy_id.rpartition("_v")
    if marker and head and tail.isdigit():
        return f"v{tail}"
    return "unversioned"


@dataclass(frozen=True, slots=True)
class CostDecisionEvidence:
    """JSON-safe snapshot of the pre-sizing CostGate verdict."""

    approved: bool | None
    profile: str
    expected_net_bps: str | None = None
    total_cost_bps: str | None = None
    min_required_bps: str | None = None
    reason: str | None = None

    @classmethod
    def not_evaluated(cls, reason: str = "not_evaluated") -> CostDecisionEvidence:
        return cls(approved=None, profile="unreported", reason=reason)

    @classmethod
    def from_result(cls, result: Any, *, profile: str) -> CostDecisionEvidence:
        cost = getattr(result, "cost", None)
        expected_net = getattr(result, "expected_net_bps", None)
        total_cost = getattr(cost, "total_cost_bps", None)
        min_required = getattr(result, "min_required_bps", None)
        reason = getattr(result, "reason", None)
        return cls(
            approved=bool(getattr(result, "approved", False)),
            profile=str(profile),
            expected_net_bps=str(expected_net) if expected_net is not None else None,
            total_cost_bps=str(total_cost) if total_cost is not None else None,
            min_required_bps=str(min_required) if min_required is not None else None,
            reason=str(reason) if reason else None,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "profile": self.profile,
            "expected_net_bps": self.expected_net_bps,
            "total_cost_bps": self.total_cost_bps,
            "min_required_bps": self.min_required_bps,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DecisionEnvelope:
    """The immutable decision and proof minted exactly once at ARM.

    The identifier is a function only of closed decision truth.  Later quote,
    risk and venue events may reference this object but cannot re-mint it.
    """

    path_id: str
    decision_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    timeframe: str
    side: str
    decision_bar_content_hash: str
    snapshot_id: str
    permission_snapshot: FrozenPermissionSnapshot
    entry_clock: str

    def __post_init__(self) -> None:
        if self.path_id != KERNEL_PATH_ID:
            raise ValueError(f"decision envelope path must be {KERNEL_PATH_ID}")
        if self.side not in {"long", "short"}:
            raise ValueError("decision envelope side must be long or short")
        if not all(
            value.strip()
            for value in (
                self.strategy_id,
                self.strategy_version,
                self.symbol,
                self.timeframe,
                self.entry_clock,
            )
        ):
            raise ValueError("decision envelope identity fields are required")
        decision_bar = self.permission_snapshot.decision_bar
        if decision_bar.timeframe != self.timeframe:
            raise ValueError("decision envelope timeframe does not match snapshot")
        if decision_bar.content_sha256 is None:
            raise ValueError("decision envelope requires hashed decision-bar content")
        if self.decision_bar_content_hash != decision_bar.content_sha256:
            raise ValueError("decision envelope bar hash does not match snapshot")
        if self.snapshot_id != self.permission_snapshot.snapshot_id:
            raise ValueError("decision envelope snapshot_id does not match payload")
        if self.side == "long" and not self.permission_snapshot.allow_long:
            raise ValueError("decision envelope long side is not permitted by snapshot")
        if self.side == "short" and not self.permission_snapshot.allow_short:
            raise ValueError("decision envelope short side is not permitted by snapshot")
        expected = make_decision_id(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            symbol=self.symbol,
            timeframe=self.timeframe,
            decision_bar_content_hash=self.decision_bar_content_hash,
            side=self.side,
            snapshot_id=self.snapshot_id,
            entry_clock=self.entry_clock,
        )
        if self.decision_id != expected:
            raise ValueError("decision_id does not match the ARM envelope")

    @classmethod
    def create(
        cls,
        *,
        strategy_id: str,
        symbol: str,
        timeframe: str,
        side: str,
        permission_snapshot: FrozenPermissionSnapshot,
        entry_clock: str,
        strategy_version: str | None = None,
    ) -> DecisionEnvelope:
        version = strategy_version or strategy_version_from_id(strategy_id)
        content_hash = permission_snapshot.decision_bar.content_sha256
        if content_hash is None:
            raise ValueError("cannot arm without a hashed closed decision bar")
        snapshot_id = permission_snapshot.snapshot_id
        decision_id = make_decision_id(
            strategy_id=strategy_id,
            strategy_version=version,
            symbol=symbol,
            timeframe=timeframe,
            decision_bar_content_hash=content_hash,
            side=side,
            snapshot_id=snapshot_id,
            entry_clock=entry_clock,
        )
        return cls(
            path_id=KERNEL_PATH_ID,
            decision_id=decision_id,
            strategy_id=strategy_id,
            strategy_version=version,
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            decision_bar_content_hash=content_hash,
            snapshot_id=snapshot_id,
            permission_snapshot=permission_snapshot,
            entry_clock=entry_clock,
        )

    @property
    def bar_open(self) -> datetime:
        return self.permission_snapshot.decision_bar.open_time

    @property
    def candle_source(self) -> str:
        return self.permission_snapshot.decision_bar.source

    def as_dict(self) -> dict[str, object]:
        return {
            "path_id": self.path_id,
            "decision_id": self.decision_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "decision_bar_content_hash": self.decision_bar_content_hash,
            "snapshot_id": self.snapshot_id,
            "bar_open": self.bar_open.isoformat(),
            "candle_source": self.candle_source,
            "entry_clock": self.entry_clock,
            "permission_snapshot": self.permission_snapshot.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DecisionEnvelope:
        """Validate an exported ARM envelope before an operational replay/drill."""

        snapshot_payload = payload.get("permission_snapshot")
        if not isinstance(snapshot_payload, Mapping):
            raise TypeError("decision envelope permission_snapshot must be an object")
        return cls(
            path_id=str(payload["path_id"]),
            decision_id=str(payload["decision_id"]),
            strategy_id=str(payload["strategy_id"]),
            strategy_version=str(payload["strategy_version"]),
            symbol=str(payload["symbol"]),
            timeframe=str(payload["timeframe"]),
            side=str(payload["side"]),
            decision_bar_content_hash=str(payload["decision_bar_content_hash"]),
            snapshot_id=str(payload["snapshot_id"]),
            permission_snapshot=FrozenPermissionSnapshot.from_dict(snapshot_payload),
            entry_clock=str(payload["entry_clock"]),
        )


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """Provenance kept beside an intent, never inside it or sent to a venue."""

    path_id: str
    decision_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    timeframe: str
    side: str
    bar_open: datetime
    htf_snapshot_id: str | None
    permission_snapshot: FrozenPermissionSnapshot | None
    candle_source: str
    entry_clock: str
    quote_sequence: int | str | None
    bbo_ts: datetime | None
    quote_age_ms: float | None
    cost_decision: CostDecisionEvidence
    decision_envelope: DecisionEnvelope | None = None

    def __post_init__(self) -> None:
        if self.path_id != KERNEL_PATH_ID:
            raise ValueError(f"execution evidence path must be {KERNEL_PATH_ID}")
        if not self.strategy_id.strip() or not self.symbol.strip() or not self.timeframe.strip():
            raise ValueError("execution evidence strategy, symbol, and timeframe are required")
        if self.side not in {"long", "short"}:
            raise ValueError("execution evidence side must be long or short")
        bar_open = _utc(self.bar_open)
        bbo_ts = _utc(self.bbo_ts) if self.bbo_ts is not None else None
        if self.quote_age_ms is not None and self.quote_age_ms < 0:
            raise ValueError("quote_age_ms cannot be negative")
        if not self.candle_source.strip() or not self.entry_clock.strip():
            raise ValueError("execution evidence candle_source and entry_clock are required")
        if self.htf_snapshot_id is not None and (
            len(self.htf_snapshot_id) != 24
            or any(c not in "0123456789abcdef" for c in self.htf_snapshot_id.lower())
        ):
            raise ValueError("htf_snapshot_id must be a 24-character hex digest")
        if self.permission_snapshot is not None:
            if self.htf_snapshot_id != self.permission_snapshot.snapshot_id:
                raise ValueError("permission snapshot payload does not match htf_snapshot_id")
            referenced = (
                self.permission_snapshot.decision_bar,
                *self.permission_snapshot.context_bars,
            )
            if any(bar.content_sha256 is None for bar in referenced):
                raise ValueError("permission snapshot must bind hashed candle content")
        if self.decision_envelope is not None:
            envelope = self.decision_envelope
            expected_fields = (
                (self.path_id, envelope.path_id),
                (self.decision_id, envelope.decision_id),
                (self.strategy_id, envelope.strategy_id),
                (self.strategy_version, envelope.strategy_version),
                (self.symbol, envelope.symbol),
                (self.timeframe, envelope.timeframe),
                (self.side, envelope.side),
                (bar_open, envelope.bar_open),
                (self.htf_snapshot_id, envelope.snapshot_id),
                (self.permission_snapshot, envelope.permission_snapshot),
                (self.candle_source, envelope.candle_source),
                (self.entry_clock, envelope.entry_clock),
            )
            if any(actual != expected for actual, expected in expected_fields):
                raise ValueError("execution evidence attempted to rewrite ARM identity")
        object.__setattr__(self, "bar_open", bar_open)
        object.__setattr__(self, "bbo_ts", bbo_ts)

    @classmethod
    def from_decision(
        cls,
        decision: DecisionEnvelope,
        *,
        quote_sequence: int | str | None = None,
        bbo_ts: datetime | None = None,
        quote_age_ms: float | None = None,
        cost_decision: CostDecisionEvidence | None = None,
    ) -> ExecutionEvidence:
        """Append ACCEPT/APPROVE facts without changing ARM identity."""

        return cls(
            path_id=decision.path_id,
            decision_id=decision.decision_id,
            strategy_id=decision.strategy_id,
            strategy_version=decision.strategy_version,
            symbol=decision.symbol,
            timeframe=decision.timeframe,
            side=decision.side,
            bar_open=decision.bar_open,
            htf_snapshot_id=decision.snapshot_id,
            permission_snapshot=decision.permission_snapshot,
            candle_source=decision.candle_source,
            entry_clock=decision.entry_clock,
            quote_sequence=quote_sequence,
            bbo_ts=bbo_ts,
            quote_age_ms=quote_age_ms,
            cost_decision=cost_decision or CostDecisionEvidence.not_evaluated(),
            decision_envelope=decision,
        )

    @classmethod
    def create(
        cls,
        *,
        strategy_id: str,
        symbol: str,
        timeframe: str,
        bar_open: datetime,
        side: str,
        strategy_version: str | None = None,
        htf_snapshot_id: str | None = None,
        permission_snapshot: FrozenPermissionSnapshot | None = None,
        candle_source: str = "unreported",
        entry_clock: str = "unreported",
        quote_sequence: int | str | None = None,
        bbo_ts: datetime | None = None,
        quote_age_ms: float | None = None,
        cost_decision: CostDecisionEvidence | None = None,
    ) -> ExecutionEvidence:
        version = strategy_version or strategy_version_from_id(strategy_id)
        if permission_snapshot is not None:
            if htf_snapshot_id is not None and htf_snapshot_id != permission_snapshot.snapshot_id:
                raise ValueError("permission snapshot payload does not match htf_snapshot_id")
            htf_snapshot_id = permission_snapshot.snapshot_id
        # Legacy constructor retained for reduce-only/recovery code while all
        # new-risk paths move to ``DecisionEnvelope.create`` at ARM.
        legacy_payload = {
            "strategy_id": strategy_id,
            "strategy_version": version,
            "symbol": symbol,
            "timeframe": timeframe,
            "bar_open": _utc(bar_open).isoformat(),
            "side": side,
            "snapshot_id": htf_snapshot_id,
            "entry_clock": entry_clock,
        }
        import hashlib
        import json

        decision_id = "dec_legacy_" + hashlib.sha256(
            json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        return cls(
            path_id=KERNEL_PATH_ID,
            decision_id=decision_id,
            strategy_id=strategy_id,
            strategy_version=version,
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            bar_open=bar_open,
            htf_snapshot_id=htf_snapshot_id,
            permission_snapshot=permission_snapshot,
            candle_source=candle_source,
            entry_clock=entry_clock,
            quote_sequence=quote_sequence,
            bbo_ts=bbo_ts,
            quote_age_ms=quote_age_ms,
            cost_decision=cost_decision or CostDecisionEvidence.not_evaluated(),
            decision_envelope=None,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "path_id": self.path_id,
            "decision_id": self.decision_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "bar_open": self.bar_open.isoformat(),
            "htf_snapshot_id": self.htf_snapshot_id,
            "permission_snapshot": (
                self.permission_snapshot.as_dict()
                if self.permission_snapshot is not None
                else None
            ),
            "candle_source": self.candle_source,
            "entry_clock": self.entry_clock,
            "decision_bar_content_hash": (
                self.decision_envelope.decision_bar_content_hash
                if self.decision_envelope is not None
                else None
            ),
            "arm_envelope": (
                self.decision_envelope.as_dict()
                if self.decision_envelope is not None
                else None
            ),
            "execution_contract_id": self.execution_contract_id,
            "quote_sequence": self.quote_sequence,
            "bbo_ts": self.bbo_ts.isoformat() if self.bbo_ts is not None else None,
            "quote_age_ms": self.quote_age_ms,
            "cost_decision": self.cost_decision.as_dict(),
        }

    @property
    def execution_contract_id(self) -> str:
        """Cohort key without overloading execution-path provenance.

        ``path_id`` remains the stable kernel spine identifier used by
        readiness and P&L eligibility.  Candle transport and entry clock are
        separate experimental dimensions and are combined only for reporting.
        """

        return f"{self.path_id}|{self.candle_source}|{self.entry_clock}"
