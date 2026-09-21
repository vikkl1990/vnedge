"""Read-only Delta India private websocket evidence collector.

This collector authenticates and captures native order, fill and position
updates. It has no order adapter and cannot declare a live capital path ready:
the fast fill channel omits commission and a reconnect requires independent
REST reconciliation before continuity can be trusted again.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from vnedge.data.symbols import canonical_symbol

logger = logging.getLogger(__name__)

DELTA_INDIA_PRIVATE_WS_URL = "wss://socket.india.delta.exchange"
_CHANNELS = ("orders", "v2/user_trades", "positions")


class DeltaPrivateContinuityError(RuntimeError):
    """Private evidence was lost or cannot be proven complete."""


@dataclass(frozen=True, slots=True)
class DeltaPrivateEvent:
    channel: str
    symbol: str
    action: str
    sequence: int | None
    payload: dict[str, Any]
    commission_complete: bool


def key_auth_payload(api_key: str, api_secret: str, *, timestamp: int) -> dict[str, Any]:
    """Official key-auth signature; secret never enters a log or journal."""
    if not api_key or not api_secret or timestamp <= 0:
        raise ValueError("Delta private authentication requires key, secret and epoch seconds")
    message = f"GET{timestamp}/live".encode()
    signature = hmac.new(api_secret.encode(), message, hashlib.sha256).hexdigest()
    return {
        "type": "key-auth",
        "payload": {"api-key": api_key, "timestamp": timestamp, "signature": signature},
    }


class DeltaPrivateWsClient:
    """Private evidence only; never an execution adapter or a readiness gate."""

    def __init__(
        self,
        symbols: tuple[str, ...],
        *,
        api_key: str,
        api_secret: str,
        on_event: Callable[[DeltaPrivateEvent], None],
        url: str = DELTA_INDIA_PRIVATE_WS_URL,
        connect: Callable[..., Any] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "wss" or parsed.hostname != "socket.india.delta.exchange":
            raise ValueError("Delta private collector requires the reviewed India host")
        normalized = tuple(dict.fromkeys(canonical_symbol(s) for s in symbols))
        if not normalized or any(not s for s in normalized):
            raise ValueError("Delta private subscriptions require exact symbols")
        if not api_key or not api_secret:
            raise ValueError("Delta private collector requires credentials")
        self.symbols = normalized
        self._key = api_key
        self._secret = api_secret
        self._on_event = on_event
        self._connect = connect
        self._wall_clock = wall_clock
        self.url = url
        self.authenticated = False
        self.connected = False
        self.continuity_ok = False
        self.last_error: str | None = None
        self._faulted = False
        self.connection_generation = 0
        self._sequences: dict[tuple[str, str], int] = {}
        self._order_snapshots: set[str] = set()
        self._position_snapshots: set[str] = set()

    @property
    def faulted(self) -> bool:
        return self._faulted

    @property
    def snapshots_complete(self) -> bool:
        expected = set(self.symbols)
        return self._order_snapshots == expected and self._position_snapshots == expected

    def subscribe_payload(self) -> dict[str, Any]:
        return {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": name, "symbols": list(self.symbols)} for name in _CHANNELS
                ]
            },
        }

    def _latch(self, reason: str) -> None:
        self.continuity_ok = False
        self.last_error = reason
        self._faulted = True
        raise DeltaPrivateContinuityError(reason)

    def ingest(self, message: dict[str, Any]) -> DeltaPrivateEvent | None:
        """Validate one authenticated message; a gap never passes silently."""
        channel = str(message.get("type") or "")
        if channel not in _CHANNELS:
            return None
        if self._faulted:
            raise DeltaPrivateContinuityError(self.last_error or "private continuity fault latched")
        if not self.authenticated:
            self._latch("private data arrived before key-auth success")
        symbol = canonical_symbol(str(message.get("symbol") or message.get("sy") or ""))
        if symbol not in self.symbols:
            self._latch("private message symbol outside subscribed roster")
        action = str(message.get("action") or "update")
        if channel in {"orders", "positions"} and action == "snapshot":
            if channel == "orders":
                self._order_snapshots.add(symbol)
            else:
                self._position_snapshots.add(symbol)
        elif channel == "orders" and symbol not in self._order_snapshots:
            self._latch(f"orders update before snapshot for {symbol}")
        elif channel == "positions" and symbol not in self._position_snapshots:
            self._latch(f"positions update before snapshot for {symbol}")

        raw_sequence = (
            message.get("se") if channel == "v2/user_trades"
            else (message.get("seq_id") or message.get("seq_no"))
        )
        if raw_sequence is None and isinstance(message.get("meta"), dict):
            raw_sequence = message["meta"].get("seq_no")
        if channel in {"orders", "v2/user_trades"}:
            if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int) or raw_sequence < 1:
                self._latch(f"{channel} missing positive sequence for {symbol}")
            key = (channel, symbol)
            previous = self._sequences.get(key)
            if previous is not None and raw_sequence != previous + 1:
                # A sequenced fast-fill frame is evidence even when it proves
                # that earlier fills were missed. Deliver it to the durable
                # trade-id sink before latching new risk; never discard an
                # observed execution merely because continuity is broken.
                if channel == "v2/user_trades":
                    gap_event = DeltaPrivateEvent(
                        channel=channel,
                        symbol=symbol,
                        action=action,
                        sequence=raw_sequence,
                        payload=dict(message),
                        commission_complete=False,
                    )
                    try:
                        self._on_event(gap_event)
                    except Exception as exc:  # noqa: BLE001 - persistence must fail closed
                        self._latch(
                            f"fast fill evidence persistence failed for {symbol}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                self._latch(
                    f"{channel} sequence discontinuity for {symbol}: "
                    f"{previous} -> {raw_sequence}"
                )
            self._sequences[key] = raw_sequence
        event = DeltaPrivateEvent(
            channel=channel,
            symbol=symbol,
            action=action,
            sequence=raw_sequence if isinstance(raw_sequence, int) else None,
            payload=dict(message),
            commission_complete=channel != "v2/user_trades",
        )
        try:
            self._on_event(event)
        except Exception as exc:
            if channel == "v2/user_trades":
                self._latch(
                    f"fast fill evidence persistence failed for {symbol}: "
                    f"{type(exc).__name__}: {exc}"
                )
            raise
        return event

    async def run_forever(
        self,
        *,
        stop_event: asyncio.Event,
        retry_delay_seconds: float = 5.0,
    ) -> None:
        """Reconnect with a new auth; each reconnect invalidates continuity."""
        if retry_delay_seconds <= 0:
            raise ValueError("retry delay must be positive")
        if self._connect is None:
            from websockets.asyncio.client import connect
        else:
            connect = self._connect
        while not stop_event.is_set():
            if self._faulted:
                return
            self.authenticated = False
            self.connected = False
            self.continuity_ok = False
            self._sequences.clear()
            self._order_snapshots.clear()
            self._position_snapshots.clear()
            try:
                async with connect(self.url) as ws:
                    self.connection_generation += 1
                    self.connected = True
                    await ws.send(json.dumps(key_auth_payload(
                        self._key, self._secret, timestamp=int(self._wall_clock())
                    )))
                    auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    if auth.get("type") != "key-auth" or auth.get("success") is not True:
                        self._latch("Delta key-auth was not confirmed")
                    self.authenticated = True
                    await ws.send(json.dumps(self.subscribe_payload()))
                    while not stop_event.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=35.0)
                        message = json.loads(raw)
                        if isinstance(message, dict):
                            self.ingest(message)
            except asyncio.CancelledError:
                raise
            except DeltaPrivateContinuityError as exc:
                self.connected = False
                self.authenticated = False
                self.continuity_ok = False
                logger.error("Delta private continuity halted: %s", exc)
                return
            except Exception as exc:  # noqa: BLE001 - all private faults fail closed
                self.connected = False
                self.authenticated = False
                self.continuity_ok = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.error("Delta private evidence unavailable: %s", self.last_error)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=retry_delay_seconds)
                except TimeoutError:
                    pass
            else:
                self.connected = False
                self.authenticated = False
                self.continuity_ok = False
