"""Cash-first arithmetic for linear contracts; no execution or tariff authority.

Rates are estimates. Reported fill fees are facts and must not be recomputed
from a tariff. All reconciliation inputs must already share one quote currency;
FX conversion requires separate, evidenced normalization before this boundary.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Literal


def finite_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("boolean is not a cash amount")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("cash amounts must be finite")
    return result


def fee_cash(notional: object, all_in_bps: object) -> Decimal:
    """Per-fill estimate, in notional currency. Rate already includes any tax."""
    return abs(finite_decimal(notional)) * finite_decimal(all_in_bps) / Decimal(10_000)


def contracts_to_base(contracts: object, contract_value: object) -> Decimal:
    """Explicit linear-contract boundary. Never multiply an already-base fill."""
    quantity, multiplier = finite_decimal(contracts), finite_decimal(contract_value)
    if quantity <= 0 or multiplier <= 0:
        raise ValueError("contracts and contract_value must be positive")
    return quantity * multiplier


@dataclass(frozen=True, slots=True)
class ChargedFill:
    """One normalized statement/simulation fill, not an order-level estimate.

    ``fee_paid`` is signed (rebates negative). If it includes tax, tax_paid
    must be absent; otherwise an explicit separate tax amount is required.
    Zero is evidence; None means missing, not free. source_ref identifies the
    original record; it is attribution, not cryptographic verification.
    """

    fill_id: str
    venue: str
    account_id: str
    symbol: str
    decision_id: str
    quote_currency: str
    charge_currency: str
    side: Literal["buy", "sell"]
    leg: Literal["open", "close"]
    quantity_base: Decimal
    price: Decimal
    fee_paid: Decimal
    fee_includes_tax: bool
    tax_paid: Decimal | None
    source_ref: str
    basis: Literal["venue_reported", "simulated"]

    def __post_init__(self) -> None:
        for name in ("fill_id", "venue", "account_id", "symbol", "decision_id",
                     "quote_currency", "charge_currency", "source_ref"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if self.side not in {"buy", "sell"} or self.leg not in {"open", "close"}:
            raise ValueError("invalid fill side or leg")
        if self.basis not in {"venue_reported", "simulated"}:
            raise ValueError("estimates are not charged fills")
        if type(self.fee_includes_tax) is not bool:
            raise ValueError("fee_includes_tax must be explicit bool")
        if self.quote_currency != self.charge_currency:
            raise ValueError("charge currency needs evidenced FX normalization")
        for name in ("quantity_base", "price", "fee_paid"):
            object.__setattr__(self, name, finite_decimal(getattr(self, name)))
        if self.quantity_base <= 0 or self.price <= 0:
            raise ValueError("fill quantity and price must be positive")
        if self.fee_includes_tax:
            if self.tax_paid is not None:
                raise ValueError("tax already included; separate tax would double count")
        elif self.tax_paid is None:
            raise ValueError("separate tax charge is missing")
        else:
            object.__setattr__(self, "tax_paid", finite_decimal(self.tax_paid))

    @property
    def charges(self) -> Decimal:
        return self.fee_paid + (self.tax_paid or Decimal(0))


@dataclass(frozen=True, slots=True)
class CashReconciliation:
    basis: str
    currency: str
    fill_count: int
    entry_notional: Decimal
    gross_cash: Decimal
    charges_cash: Decimal
    funding_paid: Decimal | None
    net_before_funding: Decimal
    net_cash: Decimal | None
    net_bps: Decimal | None


def reconcile_cash_fills(
    fills: Sequence[ChargedFill], *, funding_paid: Decimal | None,
) -> CashReconciliation:
    """Fold one flat round trip in event order, including partial executions.

    Identical fill retries dedupe; conflicting IDs fail. A non-flat position is
    deliberately refused rather than misreporting its cash flow as realized
    PnL. Funding is signed cash in the SAME currency (positive paid). Unknown
    funding leaves net unset. Spread/slippage/reserves cannot be charged here:
    they are already in fill prices or are pretrade assumptions, not bills.
    """
    unique: dict[str, ChargedFill] = {}
    for fill in fills:
        old = unique.get(fill.fill_id)
        if old is not None and old != fill:
            raise ValueError("conflicting duplicate fill_id")
        unique[fill.fill_id] = fill
    rows = tuple(unique.values())
    if not rows:
        raise ValueError("no fills to reconcile")
    keys = {(f.venue, f.account_id, f.symbol, f.decision_id, f.quote_currency, f.basis)
            for f in rows}
    if len(keys) != 1:
        raise ValueError("mixed account/decision/symbol/currency/basis")
    opening_side = rows[0].side
    inventory = gross = charges = entry_notional = Decimal(0)
    for f in rows:
        notional = f.quantity_base * f.price
        if f.leg == "open":
            if f.side != opening_side:
                raise ValueError("mixed opening directions")
            inventory += f.quantity_base
            entry_notional += notional
        else:
            if f.side == opening_side:
                raise ValueError("closing direction must oppose opening")
            inventory -= f.quantity_base
            if inventory < 0:
                raise ValueError("close exceeds filled inventory")
        gross += notional if f.side == "sell" else -notional
        charges += f.charges
    if inventory != 0 or entry_notional <= 0:
        raise ValueError("incomplete round trip; open inventory is not realized PnL")
    funding = None if funding_paid is None else finite_decimal(funding_paid)
    before = gross - charges
    net = None if funding is None else before - funding
    return CashReconciliation(
        basis=rows[0].basis, currency=rows[0].quote_currency, fill_count=len(rows),
        entry_notional=entry_notional, gross_cash=gross, charges_cash=charges,
        funding_paid=funding, net_before_funding=before, net_cash=net,
        net_bps=None if net is None else net / entry_notional * Decimal(10_000),
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Read-only normalized statement audit; prints JSON, never changes a book."""
    import argparse
    import hashlib
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("statement", type=Path)
    args = parser.parse_args(argv)
    raw = args.statement.read_bytes()
    payload = json.loads(raw, parse_float=Decimal)
    funding = payload.get("funding_paid")
    funding_source = payload.get("funding_source_ref")
    if funding is not None and not (isinstance(funding_source, str) and funding_source.strip()):
        parser.error("known funding (including zero) requires funding_source_ref")
    fills = [ChargedFill(**row) for row in payload["fills"]]
    result = reconcile_cash_fills(fills, funding_paid=funding)
    first = fills[0]
    print(json.dumps({
        "schema_version": "cash_reconciliation_v1",
        "read_only": True,
        "account_statement_verified": False,
        "source_verification": "caller_supplied_normalized_records",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "venue": first.venue, "account_id": first.account_id,
        "symbol": first.symbol, "decision_id": first.decision_id,
        "funding_source_ref": funding_source,
        **asdict(result),
    }, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
