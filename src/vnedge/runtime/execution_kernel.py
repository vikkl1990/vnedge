"""Single order-submission boundary for simulated shadow and real live modes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import ManagedOrder
from vnedge.risk.risk_manager import AccountState, MarketState, OrderIntent, RiskDecision
from vnedge.runtime.execution_contract import (
    AdapterKind,
    ExecutionContext,
    ExecutionStage,
)


@dataclass(slots=True)
class ExecutionKernel:
    """The only runtime facade allowed to submit a risk-changing order.

    Risk remains inside ``OrderManager``.  The kernel adds the authority check
    that used to be scattered across runner branches.  Both shadow execution
    and live execution call this exact method; only their adapter differs.
    """

    context: ExecutionContext
    order_manager: OrderManager
    adapter_kind: AdapterKind

    def __post_init__(self) -> None:
        self.context.validate_adapter(self.adapter_kind)

    def evaluate_candidate(
        self,
        intent: OrderIntent,
        account: AccountState,
        market: MarketState,
        *,
        now: datetime | None = None,
    ) -> RiskDecision:
        """Evaluate a non-submitting candidate through the same risk boundary."""
        return self.order_manager.evaluate_candidate(intent, account, market, now=now)

    async def submit(
        self,
        intent: OrderIntent,
        account: AccountState,
        market: MarketState,
        intent_key: str,
        *,
        now: datetime | None = None,
        replaces: str | None = None,
    ) -> ManagedOrder:
        if not self.context.stage.can_submit_orders:
            raise PermissionError(
                "observe stage cannot submit orders; emit a candidate/evidence record instead"
            )
        if (
            self.context.stage is ExecutionStage.EMERGENCY_REDUCE_ONLY
            and not intent.reduce_only
        ):
            raise PermissionError("emergency reduce-only stage refuses risk-increasing orders")
        return await self.order_manager.submit(
            intent,
            account,
            market,
            intent_key,
            now=now,
            replaces=replaces,
        )
