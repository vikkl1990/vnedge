"""Delta India execution adapter — native, maker-first, gated.

CCXT remains useful for VNEDGE's public/research plumbing, but Delta India
live execution is deliberately native: CCXT has no Delta Pro websocket, no
Delta funding-history surface, and its unified execution/sandbox abstraction
is not the contract we want for India-domiciled real orders. This adapter uses
the official ``delta-rest-client`` call shape with explicit
``post_only``/``reduce_only`` on the official India production environment:
``https://api.india.delta.exchange``.

Same ExecutionAdapter protocol + safety posture as ``CcxtExecutionAdapter``:

- **Production-data, dry-run by default.** Testnet/sandbox execution is
  refused because its liquidity, queues, and matching behavior are not valid
  scalper evidence. Real orders require BOTH real credentials AND
  ``dry_run=False`` AND ``live_confirmed=True`` — set only by the live trader
  after the three-gate settings check. No path reaches mainnet by accident.
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
from dataclasses import dataclass
import logging

from vnedge.execution.order_manager import AdapterRejection, AdapterTimeout
from vnedge.execution.order_state import ManagedOrder

logger = logging.getLogger(__name__)

_INDIA_BASE = "https://api.india.delta.exchange"


@dataclass(frozen=True)
class _DeltaEnumValue:
    """Tiny enum-compatible shim for the official delta-rest-client.

    The client expects ``order_type.value`` and ``time_in_force.value`` but we
    keep this adapter import-light so dry-run/test paths do not need to import
    the network client eagerly.
    """

    value: str


class DeltaRestExecutionAdapter:
    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
        live_confirmed: bool = False,
        dry_run: bool | None = None,
        base_url: str | None = None,
        product_ids: dict[str, int] | None = None,
        max_submit_attempts: int = 2,
        client: object | None = None,  # injectable for tests
    ) -> None:
        # dry_run defaults ON unless the caller explicitly opts into real orders
        self.dry_run = True if dry_run is None else bool(dry_run)
        candidate_base_url = base_url or _INDIA_BASE
        if testnet or "testnet" in candidate_base_url.lower():
            raise ValueError(
                "Delta testnet execution is disabled: use production market data "
                "with dry_run/shadow, then live_confirmed mainnet only after gates"
            )
        if not self.dry_run:
            if not api_key or not api_secret:
                raise ValueError(
                    "real Delta orders require trade-only credentials (or dry_run=True)"
                )
            if not live_confirmed:
                raise ValueError(
                    "mainnet execution requires live_confirmed=True — only the live "
                    "trader sets this, after the three-gate settings check"
                )
        self.testnet = testnet
        self.max_submit_attempts = max_submit_attempts
        self._product_ids = dict(product_ids or {})
        self._client = client
        self._base_url = candidate_base_url
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
        order_type = _order_type(intent.order_type)
        post_only = "true" if intent.time_in_force == "PO" else "false"
        time_in_force = _time_in_force(intent.time_in_force)
        if order_type.value == "market_order" and post_only == "true":
            raise AdapterRejection("Delta market orders cannot be post_only")
        reduce_only = "true" if intent.reduce_only else "false"
        args = dict(
            product_id=self._product_id(intent.symbol),
            size=int(intent.quantity),          # Delta sizes in integer contracts
            side=side,
            limit_price=intent.limit_price,
            order_type=order_type,
            time_in_force=time_in_force,
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
                oid = _order_id(result)
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
            oid = _order_id(res)
            return str(oid) if oid else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-timeout verification failed: %s", exc)
            return None

    async def cancel_order(self, order: ManagedOrder) -> str:
        """Cancel a working Delta order and return the venue's terminal-ish state."""

        if self.dry_run:
            return "cancelled"
        client = self._ensure_client()
        order_id = order.exchange_order_id or await self._verify_by_client_id(order)
        if order_id is None:
            return "cancelled"
        try:  # pragma: no cover - network
            result = await asyncio.to_thread(
                client.cancel_order,
                self._product_id(order.intent.symbol),
                order_id,
            )
            return _normalise_delta_status(result, default="cancelled")
        except Exception as exc:  # noqa: BLE001
            if _looks_not_found(exc):
                status = await self.fetch_order_status(order)
                if status is None:
                    return "cancelled"
                return _normalise_delta_status(status)
            raise AdapterRejection(f"Delta cancel rejected: {exc}") from exc

    async def fetch_order_status(self, order: ManagedOrder) -> dict | None:
        """Fetch venue truth by idempotent client id for reconciliation."""

        if self.dry_run:
            return None
        client = self._ensure_client()
        try:  # pragma: no cover - network
            result = await asyncio.to_thread(
                client.get_order_by_client_id,
                order.client_order_id,
            )
        except Exception as exc:  # noqa: BLE001
            if _looks_not_found(exc):
                return None
            raise
        payload = _unwrap_result(result)
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        return payload


def _order_type(raw: str) -> _DeltaEnumValue:
    value = str(raw or "").lower()
    if value in {"limit", "limit_order"}:
        return _DeltaEnumValue("limit_order")
    if value in {"market", "market_order"}:
        return _DeltaEnumValue("market_order")
    raise AdapterRejection(f"unsupported Delta order_type: {raw}")


def _time_in_force(raw: str | None) -> _DeltaEnumValue | None:
    if raw is None or raw == "" or raw == "PO":
        return None
    value = str(raw).lower()
    if value in {"gtc", "ioc", "fok"}:
        return _DeltaEnumValue(value)
    raise AdapterRejection(f"unsupported Delta time_in_force: {raw}")


def _unwrap_result(result: object) -> object:
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        return result["result"]
    return result


def _order_id(result: object) -> str | None:
    payload = _unwrap_result(result)
    if isinstance(payload, dict):
        oid = payload.get("id")
        return str(oid) if oid is not None else None
    return None


def _normalise_delta_status(result: object, *, default: str = "open") -> str:
    payload = _unwrap_result(result)
    state = ""
    if isinstance(payload, dict):
        state = str(payload.get("state") or payload.get("status") or "").lower()
    if state in {"cancelled", "canceled"}:
        return "cancelled"
    if state in {"closed", "filled"}:
        return "filled"
    if state == "open":
        return "open"
    return default


def _looks_not_found(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in ("not found", "404", "does not exist", "no order"))
