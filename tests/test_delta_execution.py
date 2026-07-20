"""Tests for the native Delta India execution adapter — safety + mapping."""
from __future__ import annotations

import asyncio

import pytest

from vnedge.exchange.delta_execution import DeltaRestExecutionAdapter
from vnedge.execution.order_manager import AdapterRejection
from vnedge.execution.order_state import ManagedOrder
from vnedge.risk.risk_manager import OrderIntent


def _order(**over):
    intent = OrderIntent(
        symbol=over.pop("symbol", "BTCUSD"),
        side=over.pop("side", "long"),
        quantity=over.pop("quantity", 5),
        notional_usd=over.pop("notional_usd", 320000.0),
        leverage=over.pop("leverage", 5.0),
        reduce_only=over.pop("reduce_only", False),
        order_type=over.pop("order_type", "limit_order"),
        limit_price=over.pop("limit_price", 64000.0),
        time_in_force=over.pop("time_in_force", "PO"),
    )
    return ManagedOrder(intent_key="k1", client_order_id="coid-1", intent=intent)


class FakeDelta:
    def __init__(self):
        self.calls = []

    def place_order(self, **kw):
        self.calls.append(kw)
        return {"id": 987654}

    def get_order_by_client_id(self, coid):
        return {"id": 111}


def _run(coro):
    return asyncio.run(coro)


def test_dry_run_is_default_and_does_not_touch_a_client():
    a = DeltaRestExecutionAdapter(product_ids={"BTCUSD": 27})
    assert a.dry_run is True
    oid = _run(a.submit_order(_order()))
    assert oid.startswith("dryrun-")


def test_real_orders_require_credentials():
    with pytest.raises(ValueError, match="credentials"):
        DeltaRestExecutionAdapter(dry_run=False, testnet=True)


def test_mainnet_requires_live_confirmed():
    with pytest.raises(ValueError, match="live_confirmed"):
        DeltaRestExecutionAdapter(
            dry_run=False, api_key="k", api_secret="s", testnet=False, live_confirmed=False,
        )
    # with the gate set, construction is allowed
    DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", testnet=False, live_confirmed=True,
        product_ids={"BTCUSD": 27},
    )


def test_maps_long_to_buy_with_post_only_and_idempotent_id():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(dry_run=False, api_key="k", api_secret="s", testnet=True,
                                  client=fake, product_ids={"BTCUSD": 27})
    oid = _run(a.submit_order(_order(side="long", time_in_force="PO")))
    assert oid == "987654"
    call = fake.calls[0]
    assert call["side"] == "buy"
    assert call["post_only"] == "true"          # maker-first
    assert call["reduce_only"] == "false"
    assert call["client_order_id"] == "coid-1"  # journaled id sent verbatim
    assert call["product_id"] == 27


def test_short_reduce_only_exit_maps_to_sell_reduce_only():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(dry_run=False, api_key="k", api_secret="s", testnet=True,
                                  client=fake, product_ids={"ETHUSD": 30})
    _run(a.submit_order(_order(symbol="ETHUSD", side="short", reduce_only=True, time_in_force=None)))
    call = fake.calls[0]
    assert call["side"] == "sell"
    assert call["reduce_only"] == "true"
    assert call["post_only"] == "false"         # not PO -> not post-only


def test_unmapped_symbol_is_rejected_not_guessed():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(dry_run=False, api_key="k", api_secret="s", testnet=True,
                                  client=fake, product_ids={})
    with pytest.raises(AdapterRejection, match="product_id"):
        _run(a.submit_order(_order()))


def test_venue_rejection_classified_as_adapter_rejection():
    class Rejecting(FakeDelta):
        def place_order(self, **kw):
            raise Exception("insufficient margin")
    a = DeltaRestExecutionAdapter(dry_run=False, api_key="k", api_secret="s", testnet=True,
                                  client=Rejecting(), product_ids={"BTCUSD": 27})
    with pytest.raises(AdapterRejection, match="rejected"):
        _run(a.submit_order(_order()))


def test_dry_run_ignores_missing_credentials():
    # dry-run must be usable with no keys at all (research/paper default)
    a = DeltaRestExecutionAdapter(product_ids={"BTCUSD": 27})
    assert _run(a.submit_order(_order())).startswith("dryrun-")
