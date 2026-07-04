"""Limit orders, cancel/replace, live reconciliation — execution completeness."""

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.live_reconciliation import LiveReconciler
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState as S
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import (
    AccountState,
    MarketState,
    OrderIntent,
    PreTradeRiskGateway,
)

SYM = "BTC/USDT:USDT"


@pytest.fixture
def world(tmp_path):
    exchange = SimulatedExchange(FillModel(), 1_000.0)
    exchange.set_quote(SYM, bid=99.99, ask=100.01)
    journal = DecisionJournal(tmp_path / "j.jsonl")
    gateway = PreTradeRiskGateway(
        RiskConfig(), KillSwitch(kill_file=tmp_path / "K")
    )
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    return om, exchange


def intent(**overrides) -> OrderIntent:
    defaults = dict(symbol=SYM, side="long", quantity=0.5, notional_usd=50.0,
                    leverage=1.0, strategy_id="test")
    defaults.update(overrides)
    return OrderIntent(**defaults)


def account() -> AccountState:
    return AccountState(equity_usd=800.0, daily_pnl_usd=0.0, peak_equity_usd=800.0,
                        open_positions=0)


def market() -> MarketState:
    return MarketState(symbol=SYM, last_update=datetime.now(UTC) - timedelta(seconds=1),
                       spread_bps=1.0, estimated_slippage_bps=2.0,
                       funding_rate=0.0001, exchange_healthy=True)


def test_gateway_rejects_limit_without_price(world):
    om, _ = world
    decision = om._gateway.evaluate(
        intent(order_type="limit"), account(), market(), now=datetime.now(UTC))
    assert not decision.approved
    assert any("limit_price" in f for f in decision.failed_checks)


async def test_limit_order_rests_then_fills_on_cross(world):
    om, exchange = world
    order = await om.submit(
        intent(order_type="limit", limit_price=95.0), account(), market(), "k1")
    assert order.state is S.ACKNOWLEDGED
    assert exchange.get_order_status(order.client_order_id).state == "open"
    exchange.set_quote(SYM, bid=94.8, ask=94.9)  # crosses the limit
    assert exchange.get_order_status(order.client_order_id).state == "filled"


async def test_cancel_working_order(world):
    om, exchange = world
    order = await om.submit(
        intent(order_type="limit", limit_price=95.0), account(), market(), "k1")
    cancelled = await om.cancel_order(order.client_order_id, "test")
    assert cancelled.state is S.CANCELLED
    assert exchange.get_open_orders() == []


async def test_cancel_loses_race_to_fill_becomes_filled(world):
    om, exchange = world
    order = await om.submit(intent(), account(), market(), "k1")  # market: fills now
    result = await om.cancel_order(order.client_order_id, "too late")
    assert result.state is S.FILLED  # the venue's answer wins


async def test_cancel_replace_happy_path(world):
    om, exchange = world
    order = await om.submit(
        intent(order_type="limit", limit_price=95.0), account(), market(), "k1")
    old, new = await om.cancel_replace(
        order.client_order_id, intent(order_type="limit", limit_price=96.0),
        account(), market(), "k2")
    assert old.state is S.CANCELLED
    assert new is not None and new.state is S.ACKNOWLEDGED
    assert exchange.get_order_status(new.client_order_id).state == "open"


async def test_cancel_replace_aborts_when_fill_won(world):
    om, _ = world
    order = await om.submit(intent(), account(), market(), "k1")  # filled
    old, new = await om.cancel_replace(
        order.client_order_id, intent(), account(), market(), "k2")
    assert old.state is S.FILLED
    assert new is None  # never double up on a filled order


# --- Live reconciler ------------------------------------------------------------

class FakeLiveAdapter:
    def __init__(self, status):
        self._status = status

    async def fetch_order_status(self, order):
        return self._status


async def stuck_order(tmp_path):
    from vnedge.execution.order_manager import AdapterTimeout

    class TimeoutAdapter:
        async def submit_order(self, order):
            raise AdapterTimeout("no ack")

    exchange = SimulatedExchange(FillModel(), 1_000.0)
    exchange.set_quote(SYM, bid=99.99, ask=100.01)
    journal = DecisionJournal(tmp_path / "j2.jsonl")
    gateway = PreTradeRiskGateway(RiskConfig(), KillSwitch(kill_file=tmp_path / "K2"))
    om = OrderManager(gateway, journal, TimeoutAdapter())
    order = await om.submit(intent(), account(), market(), "k1")
    assert order.state is S.TIMEOUT_UNKNOWN
    return om, order


@pytest.mark.parametrize("status,expected", [
    (None, S.REJECTED),
    ({"status": "closed", "filled": 0.5}, S.FILLED),
    ({"status": "open", "filled": 0.0}, S.ACKNOWLEDGED),
    ({"status": "open", "filled": 0.2}, S.PARTIALLY_FILLED),
    ({"status": "canceled", "filled": 0.0}, S.CANCELLED),
])
async def test_live_reconciler_resolves_from_venue_truth(tmp_path, status, expected):
    om, order = await stuck_order(tmp_path)
    resolved = await LiveReconciler(om, FakeLiveAdapter(status)).resolve_unknown_orders()
    assert order.client_order_id in resolved
    assert order.state is expected
    assert not om.has_unresolved_orders


async def test_live_reconciler_never_guesses_unknown_status(tmp_path):
    om, order = await stuck_order(tmp_path)
    resolved = await LiveReconciler(
        om, FakeLiveAdapter({"status": "weird_venue_state", "filled": 0})
    ).resolve_unknown_orders()
    assert resolved == []
    assert order.state is S.RECONCILING  # still blocking new risk
    assert om.has_unresolved_orders
