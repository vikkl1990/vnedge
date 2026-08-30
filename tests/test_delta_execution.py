"""Tests for the native Delta India execution adapter — safety + mapping."""
from __future__ import annotations

import asyncio

import pytest

from vnedge.exchange.delta_contracts import DeltaContractSpec
from vnedge.exchange.delta_execution import DeltaRateLimited, DeltaRestExecutionAdapter
from vnedge.execution.order_manager import AdapterRejection
from vnedge.execution.order_state import ManagedOrder
from vnedge.risk.risk_manager import OrderIntent


def _spec(symbol="BTCUSD"):
    if symbol == "ETHUSD":
        return DeltaContractSpec(
            symbol="ETHUSD", product_id=3136, contract_value=0.01,
            contract_unit_currency="ETH", tick_size=0.05,
            initial_margin_pct=0.5, maintenance_margin_pct=0.25,
        )
    return DeltaContractSpec(
        symbol="BTCUSD", product_id=27, contract_value=0.001,
        contract_unit_currency="BTC", tick_size=0.5,
        initial_margin_pct=0.5, maintenance_margin_pct=0.25,
    )


def _specs(*symbols):
    return {symbol: _spec(symbol) for symbol in symbols}


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
        self.cancel_calls = []
        self.lookup = {"coid-1": {"id": 111, "state": "open"}}

    def place_order(self, **kw):
        self.calls.append(kw)
        return {"id": 987654}

    def get_order_by_client_id(self, coid):
        if coid not in self.lookup:
            raise RuntimeError("not found")
        return self.lookup[coid]

    def cancel_order(self, product_id, order_id):
        self.cancel_calls.append((product_id, order_id))
        return {"id": order_id, "state": "cancelled"}


def _run(coro):
    return asyncio.run(coro)


def test_dry_run_is_default_and_does_not_touch_a_client():
    a = DeltaRestExecutionAdapter(
        product_ids={"BTCUSD": 27}, contract_specs=_specs("BTCUSD")
    )
    assert a.dry_run is True
    oid = _run(a.submit_order(_order()))
    assert oid.startswith("dryrun-")


def test_real_orders_require_credentials():
    with pytest.raises(ValueError, match="credentials"):
        DeltaRestExecutionAdapter(dry_run=False)


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


def test_delta_testnet_execution_is_disabled():
    with pytest.raises(ValueError, match="testnet"):
        DeltaRestExecutionAdapter(testnet=True, product_ids={"BTCUSD": 27})
    with pytest.raises(ValueError, match="testnet"):
        DeltaRestExecutionAdapter(
            base_url="https://cdn-ind.testnet.deltaex.org",
            product_ids={"BTCUSD": 27},
        )


def test_delta_india_url_defaults_to_official_production_even_for_dry_run():
    paper = DeltaRestExecutionAdapter(product_ids={"BTCUSD": 27})
    assert paper._base_url == "https://api.india.delta.exchange"

    live = DeltaRestExecutionAdapter(
        dry_run=False,
        api_key="k",
        api_secret="s",
        testnet=False,
        live_confirmed=True,
        product_ids={"BTCUSD": 27},
    )
    assert live._base_url == "https://api.india.delta.exchange"


def test_maps_long_to_buy_with_post_only_and_idempotent_id():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=fake, product_ids={"BTCUSD": 27}, contract_specs=_specs("BTCUSD"),
    )
    oid = _run(a.submit_order(_order(side="long", time_in_force="PO")))
    assert oid == "987654"
    call = fake.calls[0]
    assert call["side"] == "buy"
    assert call["post_only"] == "true"          # maker-first
    assert call["reduce_only"] == "false"
    assert call["client_order_id"] == "coid-1"  # journaled id sent verbatim
    assert call["product_id"] == 27
    assert call["order_type"].value == "limit_order"
    assert call["time_in_force"].value == "gtc"  # post-only limits remain GTC


def test_limit_price_is_directionally_snapped_and_serialized_as_decimal_string():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=fake, product_ids={"BTCUSD": 27}, contract_specs=_specs("BTCUSD"),
    )
    _run(a.submit_order(_order(side="long", limit_price=64000.37, time_in_force="PO")))
    assert fake.calls[0]["limit_price"] == "64000"


def test_fok_is_refused_before_network():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=fake, product_ids={"BTCUSD": 27}, contract_specs=_specs("BTCUSD"),
    )
    with pytest.raises(AdapterRejection, match="time_in_force"):
        _run(a.submit_order(_order(time_in_force="FOK")))
    assert fake.calls == []


def test_delta_contract_spec_converts_base_quantity_to_integer_contracts():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False,
        api_key="k",
        api_secret="s",
        live_confirmed=True,
        client=fake,
        product_ids={"ETHUSD": 3136},
        contract_specs={
            "ETHUSD": DeltaContractSpec(
                symbol="ETHUSD",
                product_id=3136,
                contract_value=0.01,
                contract_unit_currency="ETH",
                tick_size=0.05,
            )
        },
    )

    _run(
        a.submit_order(
            _order(
                symbol="ETHUSD",
                quantity=0.2,
                notional_usd=376.2,
                limit_price=1881.0,
            )
        )
    )

    assert fake.calls[0]["size"] == 20


def test_delta_contract_spec_rejects_base_quantity_below_one_contract():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False,
        api_key="k",
        api_secret="s",
        live_confirmed=True,
        client=fake,
        product_ids={"ETHUSD": 3136},
        contract_specs={
            "ETHUSD": DeltaContractSpec(
                symbol="ETHUSD",
                product_id=3136,
                contract_value=0.01,
                contract_unit_currency="ETH",
                tick_size=0.05,
            )
        },
    )

    with pytest.raises(AdapterRejection, match="below one contract"):
        _run(a.submit_order(_order(symbol="ETHUSD", quantity=0.009, limit_price=1881.0)))


def test_delta_contract_spec_market_order_uses_notional_reference_price():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False,
        api_key="k",
        api_secret="s",
        live_confirmed=True,
        client=fake,
        product_ids={"ETHUSD": 3136},
        contract_specs={
            "ETHUSD": DeltaContractSpec(
                symbol="ETHUSD",
                product_id=3136,
                contract_value=0.01,
                contract_unit_currency="ETH",
            )
        },
    )

    _run(
        a.submit_order(
            _order(
                symbol="ETHUSD",
                quantity=0.2,
                notional_usd=376.2,
                limit_price=None,
                order_type="market_order",
                time_in_force=None,
            )
        )
    )

    assert fake.calls[0]["size"] == 20


def test_short_reduce_only_exit_maps_to_sell_reduce_only():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=fake, product_ids={"ETHUSD": 3136}, contract_specs=_specs("ETHUSD"),
    )
    _run(a.submit_order(_order(symbol="ETHUSD", side="short", reduce_only=True, time_in_force=None)))
    call = fake.calls[0]
    assert call["side"] == "sell"
    assert call["reduce_only"] == "true"
    assert call["post_only"] == "false"         # not PO -> not post-only


def test_maps_delta_ioc_time_in_force_enum_shape():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=fake, product_ids={"BTCUSD": 27}, contract_specs=_specs("BTCUSD"),
    )
    _run(a.submit_order(_order(time_in_force="IOC")))
    call = fake.calls[0]
    assert call["time_in_force"].value == "ioc"


def test_rejects_nonsensical_market_post_only():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=fake, product_ids={"BTCUSD": 27},
    )
    with pytest.raises(AdapterRejection, match="post_only"):
        _run(a.submit_order(_order(order_type="market_order", time_in_force="PO")))


def test_unmapped_symbol_is_rejected_not_guessed():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=fake, product_ids={},
    )
    with pytest.raises(AdapterRejection, match="product_id"):
        _run(a.submit_order(_order()))


def test_venue_rejection_classified_as_adapter_rejection():
    class Rejecting(FakeDelta):
        def place_order(self, **kw):
            raise RuntimeError("insufficient margin")
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=Rejecting(), product_ids={"BTCUSD": 27}, contract_specs=_specs("BTCUSD"),
    )
    with pytest.raises(AdapterRejection, match="rejected"):
        _run(a.submit_order(_order()))


def test_rest_429_is_known_refusal_and_blocks_new_entries():
    class Response:
        status_code = 429

        def __init__(self):
            self.headers = {"Retry-After": "60"}

    class RateLimited(FakeDelta):
        def place_order(self, **kw):
            exc = RuntimeError("rate_limit_exceeded")
            exc.response = Response()
            raise exc

    clock = [100.0]
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=RateLimited(), product_ids={"BTCUSD": 27},
        contract_specs=_specs("BTCUSD"), wall_clock=lambda: clock[0],
    )
    with pytest.raises(DeltaRateLimited) as first:
        _run(a.submit_order(_order()))
    assert first.value.cooldown_until == 160.0
    with pytest.raises(DeltaRateLimited, match="cooldown active"):
        _run(a.submit_order(_order()))


def test_dry_run_ignores_missing_credentials():
    # dry-run must be usable with no keys at all (research/paper default)
    a = DeltaRestExecutionAdapter(
        product_ids={"BTCUSD": 27}, contract_specs=_specs("BTCUSD")
    )
    assert _run(a.submit_order(_order())).startswith("dryrun-")


def test_fetch_order_status_uses_client_order_id_for_reconciliation():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=fake, product_ids={"BTCUSD": 27},
    )
    status = _run(a.fetch_order_status(_order()))
    assert status == {"id": 111, "state": "open"}


def test_cancel_order_uses_verified_id_and_product_mapping():
    fake = FakeDelta()
    a = DeltaRestExecutionAdapter(
        dry_run=False, api_key="k", api_secret="s", live_confirmed=True,
        client=fake, product_ids={"BTCUSD": 27},
    )
    state = _run(a.cancel_order(_order()))
    assert state == "cancelled"
    assert fake.cancel_calls == [(27, "111")]
