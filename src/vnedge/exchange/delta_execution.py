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
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from vnedge.exchange.delta_contracts import (
    DeltaContractSpec,
    contracts_from_base_quantity,
)
from vnedge.exchange.delta_limit_state import parse_rate_limit_reset
from vnedge.exchange.delta_price_grid import format_delta_price, snap_limit_entry
from vnedge.execution.order_manager import AdapterRejection, AdapterTimeout
from vnedge.execution.order_state import ManagedOrder

logger = logging.getLogger(__name__)

_INDIA_BASE = "https://api.india.delta.exchange"


class DeltaRateLimited(AdapterRejection):
    """Known venue refusal. Never retry a place as an ambiguous timeout."""

    def __init__(self, message: str, *, cooldown_until: float) -> None:
        self.cooldown_until = cooldown_until
        super().__init__(message)


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
        contract_specs: dict[str, DeltaContractSpec] | None = None,
        max_submit_attempts: int = 2,
        client: object | None = None,  # injectable for tests
        wall_clock=time.time,
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
        self._contract_specs = dict(contract_specs or {})
        self._client = client
        self._base_url = candidate_base_url
        self._creds = (api_key, api_secret)
        self._wall_clock = wall_clock
        self.entries_blocked_until = 0.0

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

    def _order_contracts(self, intent) -> int:
        spec = self._contract_specs.get(intent.symbol)
        if spec is None:
            # Legacy compatibility for already-contract-shaped tests/configs.
            contracts = int(intent.quantity)
        else:
            if intent.limit_price is not None:
                price = float(intent.limit_price)
            elif intent.quantity > 0 and intent.notional_usd > 0:
                price = float(intent.notional_usd) / float(intent.quantity)
            else:
                raise AdapterRejection(
                    f"cannot convert {intent.symbol} quantity to Delta contracts without "
                    "limit_price or notional/quantity reference price"
                )
            contracts = contracts_from_base_quantity(
                base_quantity=float(intent.quantity),
                entry_price=price,
                spec=spec,
            )
        if contracts <= 0:
            raise AdapterRejection(
                f"Delta size rounds to {contracts} contracts for {intent.symbol}; "
                "quantity is below one contract after rounding down"
            )
        return contracts

    # --- ExecutionAdapter protocol -------------------------------------------
    async def submit_order(self, order: ManagedOrder) -> str:
        intent = order.intent
        if (
            not intent.reduce_only
            and self._wall_clock() < self.entries_blocked_until
        ):
            raise DeltaRateLimited(
                "Delta REST cooldown active; new entries blocked",
                cooldown_until=self.entries_blocked_until,
            )
        side = "buy" if intent.side == "long" else "sell"
        order_type = _order_type(intent.order_type)
        post_only = "true" if intent.time_in_force == "PO" else "false"
        time_in_force = _time_in_force(intent.time_in_force, order_type=order_type.value)
        if order_type.value == "market_order" and post_only == "true":
            raise AdapterRejection("Delta market orders cannot be post_only")
        reduce_only = "true" if intent.reduce_only else "false"
        limit_price = intent.limit_price
        # Resolve venue identity before checking the price grid so an unknown
        # product cannot be disguised as a tick-size failure.
        product_id = self._product_id(intent.symbol)
        spec = self._contract_specs.get(intent.symbol)
        if order_type.value == "limit_order":
            if limit_price is None:
                raise AdapterRejection("Delta limit_order requires limit_price")
            if spec is None or spec.tick_size is None:
                raise AdapterRejection(
                    f"Delta limit order requires frozen tick_size for {intent.symbol}"
                )
            snapped = snap_limit_entry(
                side=side,
                price=Decimal(str(limit_price)),
                tick=Decimal(str(spec.tick_size)),
                post_only=post_only == "true",
            )
            limit_price = format_delta_price(snapped)
        else:
            limit_price = None
        args = {
            "product_id": product_id,
            "size": self._order_contracts(intent),  # integer contracts
            "side": side,
            "limit_price": limit_price,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "client_order_id": order.client_order_id,  # verbatim idempotency key
        }
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
            except Exception as exc:
                msg = str(exc).lower()
                if _looks_rate_limited(exc):
                    headers = _response_headers(exc)
                    cooldown = parse_rate_limit_reset(headers, now=self._wall_clock())
                    self.entries_blocked_until = max(self.entries_blocked_until, cooldown)
                    raise DeltaRateLimited(
                        f"Delta REST 429; entries blocked until {self.entries_blocked_until:.3f}",
                        cooldown_until=self.entries_blocked_until,
                    ) from exc
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
        except Exception as exc:
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
        except Exception as exc:
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


def _time_in_force(raw: str | None, *, order_type: str) -> _DeltaEnumValue:
    if raw is None or raw == "":
        return _DeltaEnumValue("ioc" if order_type == "market_order" else "gtc")
    if raw == "PO":
        return _DeltaEnumValue("gtc")
    value = str(raw).lower()
    if value in {"gtc", "ioc"}:
        return _DeltaEnumValue(value)
    raise AdapterRejection(f"unsupported Delta time_in_force: {raw}")


def _response_headers(exc: Exception) -> dict[str, str]:
    response = getattr(exc, "response", None)
    raw = getattr(response, "headers", None) or getattr(exc, "headers", None) or {}
    try:
        return {str(k): str(v) for k, v in raw.items()}
    except AttributeError:
        return {}


def _looks_rate_limited(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    statuses = (
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
        getattr(response, "status_code", None),
        getattr(response, "status", None),
    )
    return 429 in statuses or "429" in str(exc) or "rate_limit" in str(exc).lower()


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
