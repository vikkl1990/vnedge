"""Read-only Delta private fill evidence and restart reconciliation.

This is not an execution adapter or a live-readiness switch. Fast websocket
fills are durably retained by fill ID before a continuity gap is latched.
REST /fills supplies the commission and missed-fill baseline on restart.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from vnedge.data.symbols import canonical_symbol
from vnedge.exchange.delta_private_ws import DeltaPrivateEvent, DeltaPrivateWsClient
from vnedge.execution.fill_ledger import FillLedger


def _positive_decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Delta fill {field} is invalid") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"Delta fill {field} must be positive")
    return result


def _identity(raw: Mapping[str, Any], *, fast: bool) -> tuple[str, str, str, str]:
    trade_id = str(raw.get("f" if fast else "id") or "").strip()
    raw_symbol = raw.get("sy" if fast else "product_symbol")
    symbol = canonical_symbol(str(raw_symbol or ""))
    price = _positive_decimal(raw.get("p" if fast else "price"), "price")
    size = _positive_decimal(raw.get("s" if fast else "size"), "size")
    if not trade_id or not symbol:
        raise ValueError("Delta fill requires trade_id and symbol")
    return (
        trade_id, symbol,
        format(price.normalize(), "f"),
        format(size.normalize(), "f"),
    )


@dataclass(frozen=True, slots=True)
class DeltaRestBaseline:
    """A completed, paginated read baseline, never inferred from WS sequence."""

    order_snapshots: Mapping[str, Sequence[Mapping[str, Any]]]
    position_snapshots: Mapping[str, Sequence[Mapping[str, Any]]]
    fills: Sequence[Mapping[str, Any]]
    fills_pagination_exhausted: bool


class DeltaPrivateRestBaselineReader:
    """Read-only snapshot + complete cursor walk through native REST /fills.

    A client is injected by the caller; construction never opens a venue
    connection or grants permission. A repeated cursor or page budget failure
    refuses the baseline instead of claiming missing fills do not exist.
    """

    def __init__(
        self, client: Any, *, product_ids: Mapping[str, int],
        max_fill_pages: int = 100,
    ) -> None:
        self._client = client
        self._product_ids = {
            canonical_symbol(symbol): int(product_id)
            for symbol, product_id in product_ids.items()
        }
        if not self._product_ids or any(pid <= 0 for pid in self._product_ids.values()):
            raise ValueError("Delta REST baseline requires frozen product IDs")
        if max_fill_pages < 1:
            raise ValueError("Delta REST fill page budget must be positive")
        self._max_fill_pages = max_fill_pages

    def read(self) -> DeltaRestBaseline:
        orders: dict[str, Sequence[Mapping[str, Any]]] = {}
        positions: dict[str, Sequence[Mapping[str, Any]]] = {}
        for symbol, product_id in self._product_ids.items():
            raw_orders = self._client.get_live_orders(query={"product_id": product_id})
            raw_position = self._client.get_position(product_id)
            if not isinstance(raw_orders, list):
                raise TypeError(f"Delta REST orders snapshot invalid for {symbol}")
            if raw_position is None:
                raise ValueError(f"Delta REST position snapshot absent for {symbol}")
            orders[symbol] = raw_orders
            positions[symbol] = (
                raw_position if isinstance(raw_position, list) else [raw_position]
            )
        fills: list[Mapping[str, Any]] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(self._max_fill_pages):
            response = self._client.fills(query={}, page_size=100, after=cursor)
            if not isinstance(response, dict) or response.get("success") is not True:
                raise ValueError("Delta REST /fills read failed")
            result = response.get("result")
            meta = response.get("meta")
            if not isinstance(result, list) or not isinstance(meta, dict):
                raise TypeError("Delta REST /fills pagination proof missing")
            fills.extend(result)
            next_cursor = meta.get("after")
            if next_cursor is None:
                return DeltaRestBaseline(orders, positions, fills, True)
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise ValueError("Delta REST /fills cursor repeated or invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise ValueError("Delta REST /fills page budget exhausted before genesis")


class DeltaPrivateFillEvidenceStore:
    """Append-only, trade-ID idempotent fill/fee evidence; no order submission."""

    def __init__(self, path: str | Path, *, symbols: Sequence[str]) -> None:
        self._ledger = FillLedger(path)
        self.symbols = frozenset(canonical_symbol(symbol) for symbol in symbols)
        if not self.symbols or "" in self.symbols:
            raise ValueError("Delta private evidence requires exact symbols")
        self._fills: dict[str, tuple[str, str, str]] = {}
        self._fee_bound: set[str] = set()
        self._fees: dict[str, str] = {}
        self.healthy = False
        if self._ledger.path.exists():
            with self._ledger.path.open() as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    kind = row.get("record_type")
                    if kind in {"fast_fill", "rest_fill"}:
                        trade_id = str(row["trade_id"])
                        identity = (str(row["symbol"]), str(row["price"]), str(row["size"]))
                        previous = self._fills.get(trade_id)
                        if previous is not None and previous != identity:
                            raise ValueError("Delta persisted trade_id identity conflict")
                        self._fills[trade_id] = identity
                        if kind == "rest_fill":
                            self._fee_bound.add(trade_id)
                            self._fees[trade_id] = str(row["commission"])
                    elif kind == "fee_binding":
                        trade_id = str(row["trade_id"])
                        self._fee_bound.add(trade_id)
                        self._fees[trade_id] = str(row["commission"])

    @property
    def trade_ids(self) -> frozenset[str]:
        return frozenset(self._fills)

    @property
    def fees_complete(self) -> bool:
        return self.trade_ids == self._fee_bound

    def record_event(self, event: DeltaPrivateEvent) -> bool:
        """Persist a fast fill synchronously; other channel events are ignored."""
        if event.channel != "v2/user_trades":
            return False
        if event.commission_complete:
            raise ValueError("fast Delta fill cannot claim commission completeness")
        trade_id, symbol, price, size = _identity(event.payload, fast=True)
        if symbol not in self.symbols or symbol != event.symbol:
            raise ValueError("Delta fast fill symbol outside evidence roster")
        identity = (symbol, price, size)
        previous = self._fills.get(trade_id)
        if previous is not None:
            if previous != identity:
                raise ValueError("Delta trade_id reused with conflicting fill")
            return False
        self._ledger.append({
            "record_type": "fast_fill", "trade_id": trade_id,
            "symbol": symbol, "price": price, "size": size,
            "sequence": event.sequence, "fee_status": "commission_absent",
            "source": "v2/user_trades", "raw": dict(event.payload),
        })
        self._fills[trade_id] = identity
        self.healthy = False
        return True

    def reconcile_restart(self, baseline: DeltaRestBaseline) -> bool:
        """Bind REST fees and missed fills only after all baseline reads complete.

        The caller must prove that REST /fills pagination was exhausted. Even a
        fully populated fee baseline is not a capital-path readiness decision.
        """
        self.healthy = False
        if not baseline.fills_pagination_exhausted:
            raise ValueError("Delta REST /fills pagination incomplete")
        for symbol in self.symbols:
            if symbol not in baseline.order_snapshots or symbol not in baseline.position_snapshots:
                raise ValueError(f"Delta restart snapshot missing for {symbol}")
        rest_by_id: dict[
            str, tuple[tuple[str, str, str], str, Mapping[str, Any]]
        ] = {}
        for raw in baseline.fills:
            trade_id, symbol, price, size = _identity(raw, fast=False)
            if symbol not in self.symbols:
                continue
            commission = raw.get("commission")
            if commission is None:
                raise ValueError(f"Delta REST commission absent for {trade_id}")
            try:
                fee = Decimal(str(commission))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"Delta REST commission invalid for {trade_id}") from exc
            if not fee.is_finite():
                raise ValueError(f"Delta REST commission invalid for {trade_id}")
            identity = (symbol, price, size)
            previous = rest_by_id.get(trade_id)
            if previous is not None and previous[:2] != (identity, str(fee)):
                raise ValueError("Delta REST duplicate trade_id conflict")
            rest_by_id[trade_id] = (identity, str(fee), raw)
        if not self.trade_ids.issubset(rest_by_id):
            raise ValueError("Delta REST /fills missing observed websocket trade_id")
        for trade_id, (identity, fee, _raw) in rest_by_id.items():
            previous = self._fills.get(trade_id)
            if previous is not None and previous != identity:
                raise ValueError("Delta WS/REST fill identity mismatch")
            old_fee = self._fees.get(trade_id)
            if old_fee is not None and Decimal(old_fee) != Decimal(fee):
                raise ValueError("Delta REST commission changed for trade_id")
        for trade_id, (identity, fee, raw) in rest_by_id.items():
            previous = self._fills.get(trade_id)
            if previous is None:
                self._ledger.append({
                    "record_type": "rest_fill", "trade_id": trade_id,
                    "symbol": identity[0], "price": identity[1], "size": identity[2],
                    "commission": fee, "fee_source": "rest_fills",
                    "raw": dict(raw),
                })
                self._fills[trade_id] = identity
                self._fee_bound.add(trade_id)
                self._fees[trade_id] = fee
            elif trade_id not in self._fee_bound:
                self._ledger.append({
                    "record_type": "fee_binding", "trade_id": trade_id,
                    "commission": fee, "fee_source": "rest_fills",
                    "raw": dict(raw),
                })
                self._fee_bound.add(trade_id)
                self._fees[trade_id] = fee
        self.healthy = self.fees_complete
        return self.healthy


class DeltaPrivateTruthObserver:
    """Coordinate WS evidence and REST restart proof without order authority."""

    def __init__(
        self,
        socket: DeltaPrivateWsClient,
        evidence: DeltaPrivateFillEvidenceStore,
        baseline_reader: DeltaPrivateRestBaselineReader,
        *,
        reconcile_interval_seconds: float = 5.0,
    ) -> None:
        if reconcile_interval_seconds <= 0:
            raise ValueError("Delta private reconcile interval must be positive")
        if frozenset(socket.symbols) != evidence.symbols:
            raise ValueError("Delta private socket/evidence roster mismatch")
        self.socket = socket
        self.evidence = evidence
        self.baseline_reader = baseline_reader
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self.last_error: str | None = None
        self._baseline_generation: int | None = None

    @property
    def healthy(self) -> bool:
        return bool(
            self.socket.connected
            and self.socket.authenticated
            and not self.socket.faulted
            and self.socket.snapshots_complete
            and self.socket.continuity_ok
            and self.evidence.healthy
            and self._baseline_generation == self.socket.connection_generation
        )

    async def reconcile_once(self) -> bool:
        """Prove one socket generation; sequence progress alone is insufficient."""
        self.socket.continuity_ok = False
        self.evidence.healthy = False
        if (
            not self.socket.connected
            or not self.socket.authenticated
            or self.socket.faulted
            or not self.socket.snapshots_complete
        ):
            self.last_error = "private order/position snapshots incomplete"
            return False
        generation = self.socket.connection_generation
        try:
            baseline = await asyncio.to_thread(self.baseline_reader.read)
            if (
                generation != self.socket.connection_generation
                or not self.socket.connected
                or self.socket.faulted
                or not self.socket.snapshots_complete
            ):
                self.last_error = "private socket generation changed during REST baseline"
                return False
            if not self.evidence.reconcile_restart(baseline):
                self.last_error = "private fill/fee baseline incomplete"
                return False
        except Exception as exc:  # noqa: BLE001 - any baseline fault fails closed
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self._baseline_generation = generation
        self.socket.continuity_ok = True
        self.last_error = None
        return self.healthy

    async def run_forever(self, *, stop_event: asyncio.Event) -> None:
        """Run the collector and repeatedly reconcile any unproved generation."""
        socket_task = asyncio.create_task(self.socket.run_forever(stop_event=stop_event))
        try:
            while not stop_event.is_set():
                if socket_task.done():
                    await socket_task
                    return
                if not self.healthy:
                    await self.reconcile_once()
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.reconcile_interval_seconds
                    )
                except TimeoutError:
                    pass
        finally:
            if not socket_task.done():
                socket_task.cancel()
            try:
                await socket_task
            except asyncio.CancelledError:
                pass
            self.socket.continuity_ok = False
            self.evidence.healthy = False
