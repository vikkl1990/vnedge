"""Order manager — the only path from an approved intent to a venue.

Submission pipeline (every step journaled before the next runs):

    intent -> duplicate check -> journal availability -> unresolved-order
    check -> pre-trade risk gateway -> mint client_order_id -> journal the
    intent -> submit via adapter -> ack / reject / TIMEOUT_UNKNOWN

Hard rules enforced here:
- No adapter call ever happens for an intent the risk gateway rejected.
- The client_order_id is journaled BEFORE the venue can possibly know it —
  crash recovery can always tell which orders might exist.
- While ANY order is TIMEOUT_UNKNOWN/RECONCILING, risk-increasing orders are
  rejected; reduce-only exits still flow (getting out is never blocked).
- Journal unavailable => same policy: exits only.
- A timed-out submission is never retried blindly; it parks in
  TIMEOUT_UNKNOWN until reconciliation resolves it via `resolve_order`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Protocol

from vnedge.execution.idempotency import IntentRegistry, mint_client_order_id
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_state import ManagedOrder, OrderState
from vnedge.risk.risk_manager import (
    AccountState,
    MarketState,
    OrderIntent,
    PreTradeRiskGateway,
)

logger = logging.getLogger(__name__)


class AdapterRejection(Exception):
    """Venue definitively rejected the order (insufficient margin, bad
    symbol...). Terminal for this order."""


class AdapterTimeout(Exception):
    """Submission MAY have reached the venue — final state unknown."""


class ExecutionAdapter(Protocol):
    async def submit_order(self, order: ManagedOrder) -> str:
        """Submit; returns the exchange order id. Raises AdapterRejection or
        AdapterTimeout. Retries (with the SAME client_order_id) live inside
        adapter implementations, never here."""
        ...


class OrderManager:
    def __init__(
        self,
        gateway: PreTradeRiskGateway,
        journal: DecisionJournal,
        adapter: ExecutionAdapter,
    ) -> None:
        self._gateway = gateway
        self._journal = journal
        self._adapter = adapter
        self._registry = IntentRegistry()
        self.orders: dict[str, ManagedOrder] = {}  # by client_order_id

    @property
    def has_unresolved_orders(self) -> bool:
        return any(o.is_unresolved for o in self.orders.values())

    async def submit(
        self,
        intent: OrderIntent,
        account: AccountState,
        market: MarketState,
        intent_key: str,
    ) -> ManagedOrder:
        """Run one intent through the full pipeline. Always returns the
        ManagedOrder — inspect .state and .history for the outcome."""
        order = ManagedOrder(
            intent_key=intent_key,
            client_order_id=mint_client_order_id(),
            intent=intent,
        )

        # --- Duplicate decision guard ---------------------------------------
        if not self._registry.register(intent_key, order.client_order_id):
            existing = self._registry.existing_order_id(intent_key)
            order.transition(OrderState.RISK_REQUESTED, "duplicate check")
            order.transition(
                OrderState.RISK_REJECTED,
                f"duplicate intent — already handled as {existing}",
            )
            self._journal.append("duplicate_intent_dropped", {
                "intent_key": intent_key, "existing_order": existing,
            })
            logger.warning("duplicate intent dropped: %s", intent_key)
            self.orders[order.client_order_id] = order
            return order

        # --- Exits-only guards (journal health, unresolved orders) ----------
        if not intent.reduce_only:
            if not self._journal.available:
                return self._refuse(order, "decision journal unavailable — exits only")
            if self.has_unresolved_orders:
                return self._refuse(
                    order,
                    "order(s) in TIMEOUT_UNKNOWN/RECONCILING — no new risk "
                    "until reconciliation resolves them",
                )

        # --- Pre-trade risk gateway ------------------------------------------
        order.transition(OrderState.RISK_REQUESTED)
        decision = self._gateway.evaluate(intent, account, market)
        self._journal.append("risk_decision", {
            "intent_key": intent_key,
            "client_order_id": order.client_order_id,
            "approved": decision.approved,
            "failed_checks": list(decision.failed_checks),
            "intent": asdict(intent),
        })
        if not decision.approved:
            order.transition(OrderState.RISK_REJECTED, decision.explanation)
            self.orders[order.client_order_id] = order
            return order
        order.transition(OrderState.RISK_APPROVED)

        # --- Journal the intent BEFORE the venue can know about it ----------
        order.transition(OrderState.ORDER_INTENT_CREATED)
        journaled = self._journal.append("order_intent", {
            "intent_key": intent_key,
            "client_order_id": order.client_order_id,
            "intent": asdict(intent),
        })
        if not journaled and not intent.reduce_only:
            # Journal died mid-pipeline: refuse to create unrecorded risk.
            order.transition(OrderState.SUBMITTING, "aborting — journal write failed")
            order.transition(OrderState.REJECTED, "journal write failed before submit")
            self.orders[order.client_order_id] = order
            return order

        # --- Submit ------------------------------------------------------------
        order.transition(OrderState.SUBMITTING)
        self.orders[order.client_order_id] = order
        try:
            exchange_id = await self._adapter.submit_order(order)
        except AdapterRejection as exc:
            order.transition(OrderState.REJECTED, f"venue rejected: {exc}")
            self._journal.append("order_rejected", {
                "client_order_id": order.client_order_id, "reason": str(exc),
            })
            return order
        except AdapterTimeout as exc:
            order.transition(OrderState.TIMEOUT_UNKNOWN, str(exc))
            self._journal.append("order_timeout_unknown", {
                "client_order_id": order.client_order_id, "detail": str(exc),
            })
            logger.critical(
                "ORDER %s TIMEOUT_UNKNOWN — blocking new risk until reconciled",
                order.client_order_id,
            )
            return order

        order.exchange_order_id = exchange_id
        order.transition(OrderState.ACKNOWLEDGED, f"exchange id {exchange_id}")
        self._journal.append("order_acknowledged", {
            "client_order_id": order.client_order_id,
            "exchange_order_id": exchange_id,
        })
        return order

    def _refuse(self, order: ManagedOrder, reason: str) -> ManagedOrder:
        order.transition(OrderState.RISK_REQUESTED, "pre-gateway guard")
        order.transition(OrderState.RISK_REJECTED, reason)
        self._journal.append("order_refused", {
            "client_order_id": order.client_order_id, "reason": reason,
        })
        logger.warning("order refused: %s", reason)
        self.orders[order.client_order_id] = order
        return order

    # --- Reconciliation hooks (driven by the reconciliation engine, m6) ------
    def begin_reconciliation(self, client_order_id: str) -> None:
        order = self.orders[client_order_id]
        order.transition(OrderState.RECONCILING, "reconciliation started")
        self._journal.append("reconciling", {"client_order_id": client_order_id})

    def resolve_order(
        self, client_order_id: str, resolved_state: OrderState, note: str
    ) -> None:
        """Resolve an unknown order to what the EXCHANGE says it is. This is
        the only exit from TIMEOUT_UNKNOWN — never assumption."""
        order = self.orders[client_order_id]
        order.transition(resolved_state, f"reconciled: {note}")
        self._journal.append("order_resolved", {
            "client_order_id": client_order_id,
            "state": resolved_state.value,
            "note": note,
        })
