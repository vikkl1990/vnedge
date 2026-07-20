"""Delta India execution adapter — native, maker-first, gated.

The CCXT adapter targets Delta *global* and treats post-only as a generic
unified flag; Delta India is a distinct venue and our surviving edges are
maker-first, so this adapter uses the OFFICIAL ``delta-rest-client`` with its
explicit ``post_only``/``reduce_only`` on ``api.india.delta.exchange``.

Same ExecutionAdapter protocol + safety posture as ``CcxtExecutionAdapter``:

- **Dry-run by default.** Real orders require BOTH real credentials AND
  ``dry_run=False``. Mainnet (``testnet=False``) additionally requires
  ``live_confirmed=True`` — set only by the live trader after the three-gate
  settings check. No path reaches mainnet by accident.
- **Idempotent by client_order_id** — the journaled id is the venue client id;
  a duplicate rejection is resolved by lookup, never by minting a new id.
- **Timeout discipline** — a network failure VERIFIES against the venue by
  client id before any bounded resubmit (same id). Still ambiguous ->
  AdapterTimeout -> TIMEOUT_UNKNOWN for reconciliation.
- Sizing/precision rounds DOWN to contract steps; the gateway upstream already
  rejected too-small results (never inflated to a minimum).
"""
from __future__ import annotations

import asyncio
import logging

from vnedge.execution.order_manager import AdapterRejection, AdapterTimeout
from vnedge.execution.order_state import ManagedOrder

logger = logging.getLogger(__name__)

_INDIA_BASE = "https://api.india.delta.exchange"


class DeltaRestExecutionAdapter:
    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
        live_confirmed: bool = False,
        dry_run: bool | None = None,
        base_url: str | None = None,
        product_ids: dict[str, int] | None = None,
        max_submit_attempts: int = 2,
        client: object | None = None,  # injectable for tests
    ) -> None:
        # dry_run defaults ON unless the caller explicitly opts into real orders
        self.dry_run = True if dry_run is None else bool(dry_run)
        if not self.dry_run:
            if not api_key or not api_secret:
                raise ValueError(
                    "real Delta orders require trade-only credentials (or dry_run=True)"
                )
            if not testnet and not live_confirmed:
                raise ValueError(
                    "mainnet execution requires live_confirmed=True — only the live "
                    "trader sets this, after the three-gate settings check"
                )
        self.testnet = testnet
        self.max_submit_attempts = max_submit_attempts
        self._product_ids = dict(product_ids or {})
        self._client = client
        self._base_url = base_url or _INDIA_BASE
        self._creds = (api_key, api_secret)

    # --- client (lazy; real construction only when not dry-run) ---------------
    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if self.dry_run:  # pragma: no cover - dry-run never builds a live client
            return None
        from delta_rest_client import DeltaRestClient  # pragma: no cover
        self._client = DeltaRestClient(
            base_url=self._base_url, api_key=self._creds[0], api_secret=self._creds[1]
        )  # pragma: no cover
        return self._client

    def _product_id(self, symbol: str) -> int:
        pid = self._product_ids.get(symbol)
        if pid is None:
            raise AdapterRejection(f"no product_id mapping for {symbol} — load products first")
        return pid

    # --- ExecutionAdapter protocol -------------------------------------------
    async def submit_order(self, order: ManagedOrder) -> str:
        intent = order.intent
        side = "buy" if intent.side == "long" else "sell"
        post_only = "true" if intent.time_in_force == "PO" else "false"
        reduce_only = "true" if intent.reduce_only else "false"
        args = dict(
            product_id=self._product_id(intent.symbol),
            size=int(intent.quantity),          # Delta sizes in integer contracts
            side=side,
            limit_price=intent.limit_price,
            order_type=intent.order_type,        # "limit_order" / "market_order" via client enum-compatible str
            post_only=post_only,
            reduce_only=reduce_only,
            client_order_id=order.client_order_id,   # idempotency key, verbatim
        )
        if self.dry_run:
            logger.info(
                "DRY-RUN Delta %s %s size=%s post_only=%s reduce_only=%s coid=%s",
                side, intent.symbol, args["size"], post_only, reduce_only,
                order.client_order_id,
            )
            return f"dryrun-{order.client_order_id}"

        client = self._ensure_client()
        for attempt in range(1, self.max_submit_attempts + 1):  # pragma: no cover - network
            try:
                result = await asyncio.to_thread(client.place_order, **args)
                oid = result.get("id") or (result.get("result") or {}).get("id")
                if oid is None:
                    raise AdapterRejection(f"venue accepted but returned no id: {result}")
                return str(oid)
            except AdapterRejection:
                raise
            except Exception as exc:  # noqa: BLE001 - classify by message (client raises requests errors)
                msg = str(exc).lower()
                if any(k in msg for k in ("duplicate", "client_order_id")):
                    existing = await self._verify_by_client_id(order)
                    if existing is not None:
                        return existing
                    raise AdapterTimeout("duplicate client id but order not found — reconcile") from exc
                if any(k in msg for k in ("insufficient", "invalid", "reduce_only", "rejected")):
                    raise AdapterRejection(f"venue rejected: {exc}") from exc
                # treat as network-ambiguous: verify before any resubmit
                logger.warning("Delta submit %s ambiguous (attempt %d/%d): %s",
                               order.client_order_id, attempt, self.max_submit_attempts, exc)
                existing = await self._verify_by_client_id(order)
                if existing is not None:
                    return existing
                if attempt == self.max_submit_attempts:
                    raise AdapterTimeout(f"submission ambiguous after {attempt} attempts") from exc
        raise AdapterTimeout("unreachable")  # pragma: no cover

    async def _verify_by_client_id(self, order: ManagedOrder) -> str | None:  # pragma: no cover - network
        if self.dry_run or self._client is None:
            return None
        await asyncio.sleep(0.5)
        try:
            res = await asyncio.to_thread(self._client.get_order_by_client_id, order.client_order_id)
            oid = res.get("id") or (res.get("result") or {}).get("id")
            return str(oid) if oid else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-timeout verification failed: %s", exc)
            return None
