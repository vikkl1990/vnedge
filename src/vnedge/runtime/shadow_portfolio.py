"""Shared, fail-closed portfolio policy for virtual observer lanes.

Observer lanes run as separate sessions, but they model one shadow account.  This
module reconstructs reservations and resolved PnL from their append-only journals
before admitting a new virtual intent.  It deliberately has no order authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    reserved_at: datetime | None = None


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
        self.portfolio_journal = DecisionJournal(
            self.journal_dir / "shadow_portfolio.journal.jsonl"
        )
        self.lock_path = self.journal_dir / "shadow_portfolio.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize snapshot + reservation across concurrent lane tasks."""
        import fcntl

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
        journal_problem = (
            not self.portfolio_journal.available
            or self.portfolio_journal.recovery_degraded
        )
        today = now.astimezone(UTC).date()

        # Reservations are written atomically before the per-lane intent WAL.
        # This closes the race where two asyncio lanes both observed an empty
        # purse and were approved before either local journal became visible.
        for record in self.portfolio_journal.read_all():
            if record.get("kind") != "shadow_reservation":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            key = str(payload.get("intent_key") or "")
            if not key:
                continue
            intents[key] = _OpenIntent(
                key=key,
                lane_id=str(payload.get("lane_id") or ""),
                symbol=str(payload.get("symbol") or ""),
                side=str(payload.get("side") or ""),
                margin_usd=max(
                    self._decimal(payload.get("margin_usd")), Decimal(0)
                ),
                reserved_at=self._record_time(
                    {"payload": {"ts": payload.get("reserved_at")}}
                ),
            )

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
                        reserved_at=None,
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

        reservation_timeout = now.astimezone(UTC) - timedelta(minutes=2)
        return (
            [
                intent
                for key, intent in intents.items()
                if key not in resolved
                and not (
                    intent.reserved_at is not None
                    and intent.reserved_at < reservation_timeout
                )
            ],
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
        intent_key: str | None = None,
    ) -> ShadowPortfolioDecision:
        """Admit a virtual intent only when shared account constraints permit it."""
        with self._locked():
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
                reason = (
                    "opposite_side_conflict" if item.side != side else "symbol_reserved"
                )
                return decision(False, reason)
            if active_margin + margin_usd > self.equity_usd:
                return decision(False, "shared_margin_exhausted")
            key = str(intent_key or "").strip()
            if key and not self.portfolio_journal.append(
                "shadow_reservation",
                {
                    "intent_key": key,
                    "lane_id": lane_id,
                    "symbol": symbol,
                    "side": side,
                    "margin_usd": str(margin_usd),
                    "reserved_at": now.astimezone(UTC).isoformat(),
                },
            ):
                return decision(False, "shadow_journal_unavailable")
            return decision(True, "approved")
