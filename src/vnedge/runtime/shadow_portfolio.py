"""Shared, fail-closed portfolio policy for virtual observer lanes.

Observer lanes run as separate sessions, but they model one shadow account.  This
module reconstructs reservations and resolved PnL from their append-only journals
before admitting a new virtual intent.  It deliberately has no order authority.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from vnedge.execution.journal import DecisionJournal


@dataclass(frozen=True, slots=True)
class ShadowPortfolioDecision:
    allowed: bool
    reason: str
    active_margin_usd: Decimal
    daily_net_usd: Decimal
    unresolved_intents: int


@dataclass(frozen=True, slots=True)
class _OpenIntent:
    key: str
    lane_id: str
    symbol: str
    side: str
    margin_usd: Decimal


class ShadowPortfolioGate:
    """Coordinate virtual lanes against one shared purse and daily halt."""

    def __init__(
        self,
        *,
        journal_dir: Path,
        lane_ids: Iterable[str],
        equity_usd: Decimal,
        daily_loss_limit_usd: Decimal,
    ) -> None:
        if equity_usd <= 0:
            raise ValueError("shadow equity must be positive")
        if daily_loss_limit_usd <= 0:
            raise ValueError("daily loss limit must be positive")
        self.journal_dir = journal_dir
        self.lane_ids = tuple(dict.fromkeys(str(value) for value in lane_ids))
        self.equity_usd = equity_usd
        self.daily_loss_limit_usd = daily_loss_limit_usd

    @staticmethod
    def _decimal(value: object, default: Decimal = Decimal(0)) -> Decimal:
        try:
            return Decimal(str(value))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _record_time(record: dict[str, object]) -> datetime | None:
        payload = record.get("payload")
        values: list[object] = []
        if isinstance(payload, dict):
            values.extend(
                payload.get(key) for key in ("bar_ts", "resolved_bar_ts", "exit_ts", "ts")
            )
        values.append(record.get("ts"))
        for value in values:
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(str(value))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        return None

    def _snapshot(self, now: datetime) -> tuple[list[_OpenIntent], Decimal, bool]:
        intents: dict[str, _OpenIntent] = {}
        resolved: set[str] = set()
        daily_net = Decimal(0)
        journal_problem = False
        today = now.astimezone(UTC).date()

        for lane_id in self.lane_ids:
            journal = DecisionJournal(self.journal_dir / f"{lane_id}.journal.jsonl")
            journal_problem = journal_problem or not journal.available or journal.recovery_degraded
            records = journal.read_all()
            for record in records:
                kind = str(record.get("kind", ""))
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                key = str(payload.get("intent_key") or "")
                if kind == "shadow_intent" and bool(payload.get("approved")) and key:
                    intent = payload.get("intent")
                    if not isinstance(intent, dict):
                        continue
                    notional = self._decimal(intent.get("notional_usd"))
                    leverage = max(self._decimal(intent.get("leverage"), Decimal(1)), Decimal(1))
                    margin = self._decimal(payload.get("margin_usd"), notional / leverage)
                    intents[key] = _OpenIntent(
                        key=key,
                        lane_id=lane_id,
                        symbol=str(intent.get("symbol", "")),
                        side=str(intent.get("side", "")),
                        margin_usd=max(margin, Decimal(0)),
                    )
                elif (
                    kind
                    in {
                        "shadow_outcome",
                        "shadow_unfilled",
                        "shadow_maker_unfilled",
                    }
                    and key
                ):
                    resolved.add(key)
                    if kind == "shadow_outcome":
                        record_time = self._record_time(record)
                        if record_time is not None and record_time.date() == today:
                            daily_net += self._decimal(payload.get("virtual_net_usd"))

        return (
            [intent for key, intent in intents.items() if key not in resolved],
            daily_net,
            journal_problem,
        )

    def evaluate_entry(
        self,
        *,
        lane_id: str,
        symbol: str,
        side: str,
        margin_usd: Decimal,
        now: datetime,
    ) -> ShadowPortfolioDecision:
        """Admit a virtual intent only when shared account constraints permit it."""
        open_intents, daily_net, journal_problem = self._snapshot(now)
        active_margin = sum((item.margin_usd for item in open_intents), Decimal(0))

        def decision(allowed: bool, reason: str) -> ShadowPortfolioDecision:
            return ShadowPortfolioDecision(
                allowed,
                reason,
                active_margin,
                daily_net,
                len(open_intents),
            )

        if journal_problem:
            return decision(False, "shadow_journal_unavailable")
        if daily_net <= -self.daily_loss_limit_usd:
            return decision(False, "shared_daily_loss_halt")
        for item in open_intents:
            if item.symbol != symbol or item.lane_id == lane_id:
                continue
            reason = "opposite_side_conflict" if item.side != side else "symbol_reserved"
            return decision(False, reason)
        if active_margin + margin_usd > self.equity_usd:
            return decision(False, "shared_margin_exhausted")
        return decision(True, "approved")
