"""Maker-first executor with fee-aware taker fallback."""

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.maker_taker_executor import (
    ExecutorState,
    MakerTakerExecutionPlan,
    MakerTakerExecutor,
)
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import AccountState, MarketState, OrderIntent, PreTradeRiskGateway
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
SYM = "BTC/USDT:USDT"


def account() -> AccountState:
    return AccountState(
        equity_usd=1_000.0,
        daily_pnl_usd=0.0,
        peak_equity_usd=1_000.0,
        open_positions=0,
        exposure_by_symbol_usd={},
        total_exposure_usd=0.0,
    )


def market() -> MarketState:
    return MarketState(
        symbol=SYM,
        last_update=NOW - timedelta(seconds=1),
        spread_bps=1.0,
        estimated_slippage_bps=1.0,
        funding_rate=0.0,
        exchange_healthy=True,
    )


def intent(**overrides) -> OrderIntent:
    defaults = dict(
        symbol=SYM,
        side="long",
        quantity=1.0,
        notional_usd=100.0,
        leverage=1.0,
        reduce_only=False,
        strategy_id="executor_test",
        order_type="limit",
        limit_price=99.99,
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


def plan(**overrides) -> MakerTakerExecutionPlan:
    defaults = dict(
        executor_id="exec_1",
        intent=intent(),
        expected_edge_bps=20.0,
        fee_profile=DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile("binanceusdm"),
        maker_ttl_ms=250,
    )
    defaults.update(overrides)
    return MakerTakerExecutionPlan(**defaults)


def world(tmp_path, *, script=None):
    journal = DecisionJournal(tmp_path / "journal.jsonl")
    exchange = SimulatedExchange(
        FillModel(slippage_bps=0.0, taker_fee_bps=0.0),
        starting_balance_usd=1_000.0,
    )
    exchange.set_quote(SYM, bid=99.99, ask=100.01)
    gateway = PreTradeRiskGateway(
        RiskConfig(max_spread_bps=10.0, max_slippage_bps=10.0),
        KillSwitch(kill_file=tmp_path / "KILL"),
    )
    om = OrderManager(gateway, journal, PaperBroker(exchange, script=script))
    return MakerTakerExecutor(om, journal), exchange, journal


async def test_maker_quote_is_post_only_and_taker_fallback_submits_when_edge_covers(tmp_path):
    executor, exchange, journal = world(tmp_path)

    report = await executor.execute(plan(), account=account(), market=market(), now=NOW)

    assert report.state is ExecutorState.TAKER_SUBMITTED
    assert report.maker_order is not None
    assert report.taker_order is not None
    assert report.maker_order.state is OrderState.CANCELLED
    assert report.maker_order.intent.time_in_force == "PO"
    assert report.taker_order.intent.order_type == "market"
    assert report.taker_order.intent.time_in_force is None
    assert len(exchange.get_fills()) == 1
    assert exchange.get_fills()[0].client_order_id == report.taker_order.client_order_id

    intents = [r for r in journal.read_all() if r["kind"] == "order_intent"]
    assert intents[0]["payload"]["intent"]["time_in_force"] == "PO"
    assert intents[1]["payload"]["intent"]["order_type"] == "market"


async def test_taker_fallback_blocks_when_edge_no_longer_covers_fees(tmp_path):
    executor, exchange, journal = world(tmp_path)

    report = await executor.execute(
        plan(),
        account=account(),
        market=market(),
        now=NOW,
        edge_at_fallback_bps=8.0,  # Binance taker round-trip hurdle is 12bps.
    )

    assert report.state is ExecutorState.TAKER_BLOCKED
    assert report.taker_order is None
    assert report.taker_check is not None
    assert not report.taker_check.allowed
    assert exchange.get_fills() == []
    assert report.reason == "taker_fallback_edge_below_hurdle"
    finished = [r for r in journal.read_all() if r["kind"] == "executor_finished"][-1]
    assert finished["payload"]["state"] == "taker_blocked"


async def test_maker_fill_race_does_not_double_submit_taker(tmp_path):
    executor, exchange, _journal = world(tmp_path)

    def fill_maker(_order):
        # Buy limit at 99.99 fills when ask touches 99.99 before cancel.
        exchange.set_quote(SYM, bid=99.98, ask=99.99)

    report = await executor.execute(
        plan(),
        account=account(),
        market=market(),
        now=NOW,
        after_maker_submit=fill_maker,
    )

    assert report.state is ExecutorState.MAKER_FILLED
    assert report.taker_order is None
    assert len(exchange.get_fills()) == 1
    assert exchange.get_fills()[0].client_order_id == report.maker_order.client_order_id


async def test_partial_maker_fill_falls_back_only_for_remaining_quantity(tmp_path):
    executor, exchange, _journal = world(tmp_path)

    def partial_fill(order):
        exchange.partial_fill(order.client_order_id, 0.4)

    report = await executor.execute(
        plan(),
        account=account(),
        market=market(),
        now=NOW,
        after_maker_submit=partial_fill,
    )

    assert report.state is ExecutorState.TAKER_SUBMITTED
    assert report.maker_filled_quantity == pytest.approx(0.4)
    assert report.taker_quantity == pytest.approx(0.6)
    assert report.taker_order is not None
    assert report.taker_order.intent.quantity == pytest.approx(0.6)
    fills = exchange.get_fills()
    assert len(fills) == 2
    assert fills[0].client_order_id == report.maker_order.client_order_id
    assert fills[1].client_order_id == report.taker_order.client_order_id
    assert fills[1].quantity == pytest.approx(0.6)


async def test_maker_fee_wall_blocks_before_any_order(tmp_path):
    executor, exchange, journal = world(tmp_path)

    report = await executor.execute(
        plan(expected_edge_bps=5.0),  # maker-first cost is 9bps on Binance profile.
        account=account(),
        market=market(),
        now=NOW,
    )

    assert report.state is ExecutorState.BLOCKED
    assert report.maker_order is None
    assert report.taker_order is None
    assert exchange.orders == {}
    kinds = [r["kind"] for r in journal.read_all()]
    assert "order_intent" not in kinds


async def test_maker_timeout_unknown_does_not_attempt_fallback(tmp_path):
    executor, exchange, _journal = world(tmp_path, script=["timeout_reached"])

    report = await executor.execute(plan(), account=account(), market=market(), now=NOW)

    assert report.state is ExecutorState.TIMEOUT_UNKNOWN
    assert report.maker_order is not None
    assert report.maker_order.state is OrderState.TIMEOUT_UNKNOWN
    assert report.taker_order is None
    assert len(exchange.orders) == 1


async def test_taker_fallback_can_be_disabled_even_when_edge_covers(tmp_path):
    executor, exchange, _journal = world(tmp_path)

    report = await executor.execute(
        plan(fallback_enabled=False),
        account=account(),
        market=market(),
        now=NOW,
    )

    assert report.state is ExecutorState.TAKER_BLOCKED
    assert report.taker_order is None
    assert report.taker_check is not None
    assert "fallback_disabled" in report.taker_check.failed_checks
    assert exchange.get_fills() == []


async def test_taker_fallback_uses_fresh_account_snapshot(tmp_path):
    executor, exchange, _journal = world(tmp_path)

    stale_account = account()
    fresh_account = AccountState(
        equity_usd=1_000.0,
        daily_pnl_usd=0.0,
        peak_equity_usd=1_000.0,
        open_positions=0,
        exposure_by_symbol_usd={SYM: 500.0},
        total_exposure_usd=500.0,
    )

    report = await executor.execute(
        plan(),
        account=stale_account,
        account_at_fallback=fresh_account,
        market=market(),
        now=NOW,
    )

    assert report.state is ExecutorState.TAKER_BLOCKED
    assert report.taker_order is not None
    assert report.taker_order.state is OrderState.RISK_REJECTED
    assert any("symbol_exposure" in event.note for event in report.taker_order.history)
    assert exchange.get_fills() == []
