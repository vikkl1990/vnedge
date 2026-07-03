"""Live/testnet execution adapter — the last link in the execution chain.

Implements the same ExecutionAdapter protocol the paper broker does, against
a real venue via CCXT. Safety posture:

- **Testnet by default.** Constructing with ``testnet=False`` additionally
  requires ``live_confirmed=True`` — wired only from the (future) live
  trader after the three-gate settings check. There is no code path that
  reaches mainnet by accident.
- **Idempotent by client_order_id**: the venue receives our journaled id as
  the client order id. A duplicate-id rejection is resolved by fetching the
  existing order — never by minting a new id.
- **Timeout discipline** (docs/DESIGN.md §2): on a network timeout the
  adapter VERIFIES against the venue by client id. Found -> acknowledged.
  Not found -> bounded resubmit with the SAME id. Still ambiguous ->
  AdapterTimeout, which parks the order in TIMEOUT_UNKNOWN for
  reconciliation. Never a blind retry, never a fresh id.
"""

from __future__ import annotations

import asyncio
import logging

from vnedge.execution.order_manager import AdapterRejection, AdapterTimeout
from vnedge.execution.order_state import ManagedOrder

logger = logging.getLogger(__name__)


class CcxtExecutionAdapter:
    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        *,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        live_confirmed: bool = False,
        max_submit_attempts: int = 2,
        client: object | None = None,  # injectable for tests
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("execution adapter requires API credentials (trade-only keys)")
        if not testnet and not live_confirmed:
            raise ValueError(
                "mainnet execution requires live_confirmed=True — only the live "
                "trader sets this, after the three-gate settings check"
            )
        self.exchange_id = exchange_id
        self.testnet = testnet
        self.max_submit_attempts = max_submit_attempts
        if client is not None:
            self._ex = client
        else:  # pragma: no cover - network client construction
            import ccxt.async_support as ccxt_async

            self._ex = getattr(ccxt_async, exchange_id)(
                {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
            )
            if testnet:
                self._ex.set_sandbox_mode(True)

    async def close(self) -> None:
        await self._ex.close()

    # --- ExecutionAdapter protocol --------------------------------------------------
    async def submit_order(self, order: ManagedOrder) -> str:
        from ccxt.base.errors import (
            DuplicateOrderId,
            ExchangeError,
            InsufficientFunds,
            InvalidOrder,
            NetworkError,
        )

        intent = order.intent
        side = "buy" if intent.side == "long" else "sell"
        params = {
            "newClientOrderId": order.client_order_id,
            "reduceOnly": intent.reduce_only,
        }

        for attempt in range(1, self.max_submit_attempts + 1):
            try:
                result = await self._ex.create_order(
                    intent.symbol, "market", side, intent.quantity, None, params
                )
                return str(result["id"])
            except DuplicateOrderId:
                # Our id already exists at the venue — idempotency working.
                existing = await self._fetch_by_client_id(order)
                if existing is not None:
                    return existing
                raise AdapterTimeout(
                    "venue reports duplicate client id but order not found — reconcile"
                )
            except (InsufficientFunds, InvalidOrder) as exc:
                raise AdapterRejection(f"{type(exc).__name__}: {exc}") from exc
            except NetworkError as exc:
                logger.warning(
                    "submit %s network error (attempt %d/%d): %s",
                    order.client_order_id, attempt, self.max_submit_attempts, exc,
                )
                # MAY have reached the venue: verify before any resubmit.
                existing = await self._verify_after_timeout(order)
                if existing is not None:
                    return existing
                if attempt == self.max_submit_attempts:
                    raise AdapterTimeout(
                        f"submission ambiguous after {attempt} attempts"
                    ) from exc
                # Not found at venue: safe to resubmit with the SAME id.
            except ExchangeError as exc:
                raise AdapterRejection(f"venue error: {exc}") from exc
        raise AdapterTimeout("unreachable")  # pragma: no cover

    async def cancel_order(self, order: ManagedOrder) -> None:
        from ccxt.base.errors import OrderNotFound

        try:
            await self._ex.cancel_order(
                order.exchange_order_id, order.intent.symbol,
                {"origClientOrderId": order.client_order_id},
            )
        except OrderNotFound:
            pass  # already gone — reconciliation confirms final state

    # --- Venue truth (reconciliation surface, same shape as the paper venue) --------
    async def fetch_order_status(self, order: ManagedOrder) -> dict | None:
        from ccxt.base.errors import OrderNotFound

        try:
            return await self._ex.fetch_order(
                None, order.intent.symbol,
                {"origClientOrderId": order.client_order_id},
            )
        except OrderNotFound:
            return None

    async def _fetch_by_client_id(self, order: ManagedOrder) -> str | None:
        result = await self.fetch_order_status(order)
        return str(result["id"]) if result else None

    async def _verify_after_timeout(self, order: ManagedOrder) -> str | None:
        await asyncio.sleep(0.5)  # give the venue a beat to register it
        try:
            return await self._fetch_by_client_id(order)
        except Exception as exc:  # noqa: BLE001 — verification itself failed
            logger.warning("post-timeout verification failed: %s", exc)
            return None
