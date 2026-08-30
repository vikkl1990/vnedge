"""Fail-closed Delta India product-spec freeze and drift detection.

The product payload uses percent units for margin fields (``0.5`` means
0.5%).  Only fields with a defined sizing/risk meaning are frozen.  Raw
venue metadata such as ``liquidation_penalty_factor`` is retained for audit
but is never used by the sizer or CostGate.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from vnedge.exchange.delta_contracts import DeltaContractSpec

DELTA_PRODUCT_HOST = "https://api.india.delta.exchange"
REQUIRED_PRODUCTS = ("BTCUSD", "ETHUSD")
BASELINE: dict[str, dict[str, float | int | str]] = {
    "BTCUSD": {
        "product_id": 27,
        "contract_value": 0.001,
        "tick_size": 0.5,
        "initial_margin_pct": 0.5,
        "maintenance_margin_pct": 0.25,
        "contract_type": "perpetual_futures",
    },
    "ETHUSD": {
        "product_id": 3136,
        "contract_value": 0.01,
        "tick_size": 0.05,
        "initial_margin_pct": 0.5,
        "maintenance_margin_pct": 0.25,
        "contract_type": "perpetual_futures",
    },
}
_EPS = {
    "contract_value": 1e-12,
    "tick_size": 1e-12,
    "initial_margin_pct": 1e-9,
    "maintenance_margin_pct": 1e-9,
}
_LIVE_STATES = frozenset({"live", "operational"})


class DeltaSnapshotValidationError(RuntimeError):
    def __init__(self, failures: list[str] | str) -> None:
        self.failures = [failures] if isinstance(failures, str) else list(failures)
        super().__init__("; ".join(self.failures))


class JournalSink(Protocol):
    def append(self, kind: str, payload: dict[str, Any]) -> bool: ...


@dataclass(frozen=True, slots=True)
class ValidatedDeltaSnapshot:
    fetched_at: str
    host: str
    symbol: str
    product_id: int
    contract_value: float
    tick_size: float
    initial_margin_pct: float
    maintenance_margin_pct: float
    contract_type: str
    state: str
    max_implied_leverage: float
    requested_leverage_default: float = 5.0
    leverage_ceiling: float = 30.0
    raw_liquidation_penalty_factor: str | None = None
    raw_default_leverage: str | None = None

    def to_contract_spec(self) -> DeltaContractSpec:
        return DeltaContractSpec(
            symbol=self.symbol,
            product_id=self.product_id,
            contract_value=self.contract_value,
            contract_unit_currency=self.symbol.removesuffix("USD"),
            tick_size=self.tick_size,
            initial_margin_pct=self.initial_margin_pct,
            maintenance_margin_pct=self.maintenance_margin_pct,
        )

    def intent_fields(
        self, *, contracts: int, base_quantity: float, requested_leverage: float
    ) -> dict[str, Any]:
        if contracts <= 0:
            raise DeltaSnapshotValidationError("contracts must be positive")
        if not 0 < requested_leverage <= self.leverage_ceiling:
            raise DeltaSnapshotValidationError(
                f"requested_leverage {requested_leverage} outside (0, {self.leverage_ceiling}]"
            )
        expected = contracts * self.contract_value
        if not math.isclose(base_quantity, expected, abs_tol=1e-12, rel_tol=0.0):
            raise DeltaSnapshotValidationError(
                f"base_quantity {base_quantity} != {contracts} * {self.contract_value}"
            )
        return {
            "product_id": self.product_id,
            "contract_value": self.contract_value,
            "tick_size": self.tick_size,
            "im_pct": self.initial_margin_pct,
            "mm_pct": self.maintenance_margin_pct,
            "requested_leverage": requested_leverage,
            "leverage_ceiling": self.leverage_ceiling,
            "contracts": int(contracts),
            "base_quantity": float(base_quantity),
            "spec_fetched_at": self.fetched_at,
            "spec_host": self.host,
        }


def normalize_delta_native_symbol(symbol: str) -> str:
    text = str(symbol).strip().upper().replace(" ", "")
    if "USDT" in text:
        raise DeltaSnapshotValidationError(f"USDT symbol is not Delta-native: {symbol}")
    if text in {"BTC/USD:USD", "BTCUSD:USD"}:
        return "BTCUSD"
    if text in {"ETH/USD:USD", "ETHUSD:USD"}:
        return "ETHUSD"
    return text.replace("/", "").replace(":USD", "")


def _number(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DeltaSnapshotValidationError(f"{key} is not numeric: {value!r}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise DeltaSnapshotValidationError(f"{key} must be finite and positive")
    return parsed


def parse_product_snapshot(
    symbol: str,
    raw: Mapping[str, Any],
    *,
    host: str = DELTA_PRODUCT_HOST,
    fetched_at: datetime | None = None,
) -> ValidatedDeltaSnapshot:
    if "testnet" in host.lower():
        raise DeltaSnapshotValidationError(f"refusing Delta testnet product host: {host}")
    native = normalize_delta_native_symbol(symbol)
    try:
        product_id = int(raw["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DeltaSnapshotValidationError("missing/invalid product id") from exc
    im_pct = _number(raw, "initial_margin")
    mm_pct = _number(raw, "maintenance_margin")
    failures: list[str] = []
    if mm_pct >= im_pct:
        failures.append(f"{native} MM {mm_pct}% >= IM {im_pct}%")
    if im_pct < 0.1:
        failures.append(f"{native} IM {im_pct} looks fraction-scaled, expected percent units")
    contract_type = str(raw.get("contract_type") or "")
    state = str(raw.get("state") or raw.get("trading_status") or "")
    if failures:
        raise DeltaSnapshotValidationError(failures)
    return ValidatedDeltaSnapshot(
        fetched_at=(fetched_at or datetime.now(UTC)).isoformat(),
        host=host,
        symbol=native,
        product_id=product_id,
        contract_value=_number(raw, "contract_value"),
        tick_size=_number(raw, "tick_size"),
        initial_margin_pct=im_pct,
        maintenance_margin_pct=mm_pct,
        contract_type=contract_type,
        state=state,
        max_implied_leverage=100.0 / im_pct,
        raw_liquidation_penalty_factor=(
            None if raw.get("liquidation_penalty_factor") is None
            else str(raw["liquidation_penalty_factor"])
        ),
        raw_default_leverage=(
            None if raw.get("default_leverage") is None else str(raw["default_leverage"])
        ),
    )


def compare_to_baseline(snapshot: ValidatedDeltaSnapshot) -> list[str]:
    expected = BASELINE.get(snapshot.symbol)
    if expected is None:
        return [f"no frozen baseline for {snapshot.symbol}"]
    failures: list[str] = []
    if snapshot.product_id != expected["product_id"]:
        failures.append(
            f"{snapshot.symbol} product_id {snapshot.product_id} != {expected['product_id']}"
        )
    if snapshot.contract_type != expected["contract_type"]:
        failures.append(
            f"{snapshot.symbol} contract_type {snapshot.contract_type!r} != "
            f"{expected['contract_type']!r}"
        )
    if snapshot.state.lower() not in _LIVE_STATES:
        failures.append(f"{snapshot.symbol} state {snapshot.state!r} is not live")
    for field, eps in _EPS.items():
        actual = float(getattr(snapshot, field))
        wanted = float(expected[field])
        if abs(actual - wanted) > eps:
            failures.append(f"{snapshot.symbol} {field} {actual} != {wanted}")
    return failures


def validate_startup_universe(
    raw_by_symbol: Mapping[str, Mapping[str, Any]],
    *,
    host: str = DELTA_PRODUCT_HOST,
) -> dict[str, ValidatedDeltaSnapshot]:
    failures: list[str] = []
    result: dict[str, ValidatedDeltaSnapshot] = {}
    normalized_input: dict[str, Mapping[str, Any]] = {}
    for symbol, raw in raw_by_symbol.items():
        normalized_input[normalize_delta_native_symbol(symbol)] = raw
    extras = set(normalized_input) - set(REQUIRED_PRODUCTS)
    if extras:
        failures.append(f"unexpected products in startup freeze: {sorted(extras)}")
    for symbol in REQUIRED_PRODUCTS:
        raw = normalized_input.get(symbol)
        if raw is None:
            failures.append(f"missing required product {symbol}")
            continue
        try:
            snapshot = parse_product_snapshot(symbol, raw, host=host)
        except DeltaSnapshotValidationError as exc:
            failures.extend(exc.failures)
            continue
        failures.extend(compare_to_baseline(snapshot))
        result[symbol] = snapshot
    if failures:
        raise DeltaSnapshotValidationError(failures)
    return result


def journal_frozen_universe(
    journal: JournalSink, snapshots: Mapping[str, ValidatedDeltaSnapshot]
) -> None:
    if set(snapshots) != set(REQUIRED_PRODUCTS):
        raise DeltaSnapshotValidationError("refusing partial Delta universe journal")
    payload = {
        "host": DELTA_PRODUCT_HOST,
        "products": [asdict(snapshots[s]) for s in REQUIRED_PRODUCTS],
    }
    if not journal.append("delta_product_specs_frozen", payload):
        raise DeltaSnapshotValidationError("decision journal unavailable during spec freeze")


def bootstrap_delta_product_specs(
    journal: JournalSink,
    *,
    fetch_product: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, ValidatedDeltaSnapshot]:
    """Fetch prod BTC/ETH once, validate, freeze sizing limits, then journal."""
    if fetch_product is None:
        from vnedge.exchange.delta_contracts import fetch_india_product_json

        fetch_product = fetch_india_product_json
    raw = {symbol: fetch_product(symbol) for symbol in REQUIRED_PRODUCTS}
    snapshots = validate_startup_universe(raw)
    from vnedge.exchange.venue_specs import freeze_delta_specs

    freeze_delta_specs({symbol: snap.to_contract_spec() for symbol, snap in snapshots.items()})
    journal_frozen_universe(journal, snapshots)
    return snapshots


@dataclass(frozen=True, slots=True)
class DeltaSpecDrift:
    symbol: str
    field: str
    frozen: Any
    live: Any
    critical: bool


_RISK_FIELDS = (
    "product_id", "contract_value", "tick_size", "initial_margin_pct",
    "maintenance_margin_pct", "contract_type", "state",
)


def diff_snapshots(
    frozen: ValidatedDeltaSnapshot, live: ValidatedDeltaSnapshot
) -> list[DeltaSpecDrift]:
    drifts: list[DeltaSpecDrift] = []
    for field in _RISK_FIELDS:
        a, b = getattr(frozen, field), getattr(live, field)
        eps = _EPS.get(field)
        changed = abs(float(a) - float(b)) > eps if eps is not None else a != b
        if changed:
            drifts.append(DeltaSpecDrift(frozen.symbol, field, a, b, True))
    for field in ("raw_liquidation_penalty_factor", "raw_default_leverage"):
        a, b = getattr(frozen, field), getattr(live, field)
        if a != b:
            drifts.append(DeltaSpecDrift(frozen.symbol, field, a, b, False))
    return drifts


class DeltaSpecDriftMonitor:
    def __init__(
        self,
        frozen: Mapping[str, ValidatedDeltaSnapshot],
        *,
        fetch_product: Callable[[str], Mapping[str, Any]],
        journal: JournalSink,
        block_new_arms: Callable[[bool], None] | None = None,
    ) -> None:
        if not frozen:
            raise ValueError("Delta drift monitor requires a frozen universe")
        self.frozen = dict(frozen)
        self.fetch_product = fetch_product
        self.journal = journal
        self.block_new_arms = block_new_arms
        self.arms_blocked = False
        self._last_fingerprint: tuple[str, ...] | None = None

    def check_once(self) -> list[DeltaSpecDrift]:
        drifts: list[DeltaSpecDrift] = []
        parse_failures: list[str] = []
        for symbol, frozen in self.frozen.items():
            try:
                live = parse_product_snapshot(symbol, self.fetch_product(symbol))
                drifts.extend(diff_snapshots(frozen, live))
            except Exception as exc:  # noqa: BLE001 - fail closed on network/schema errors
                parse_failures.append(f"{symbol}: {exc}")
        block = bool(parse_failures) or any(item.critical for item in drifts)
        fingerprint = tuple(parse_failures) + tuple(
            f"{d.symbol}:{d.field}:{d.frozen}->{d.live}:{d.critical}" for d in drifts
        )
        if fingerprint != self._last_fingerprint:
            if fingerprint and not self.journal.append(
                "delta_product_spec_drift",
                {
                    "checked_at": datetime.now(UTC).isoformat(),
                    "parse_failures": parse_failures,
                    "drifts": [asdict(item) for item in drifts],
                    "block_new_arms": block,
                },
            ):
                block = True
            self._last_fingerprint = fingerprint
        self.arms_blocked = block
        if self.block_new_arms is not None:
            self.block_new_arms(block)
        return drifts
