"""Cross-session paper account persistence — crash and resume."""

import asyncio
from datetime import UTC, datetime

import pytest

from vnedge.paper.account_store import PaperAccountStore
from vnedge.paper.fill_model import FillModel
from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange
from vnedge.runtime.portfolio_tracker import PortfolioTracker

SYM = "BTC/USDT:USDT"


def world(balance=500.0):
    ex = SimulatedExchange(FillModel(slippage_bps=0, taker_fee_bps=0), balance)
    ex.set_quote(SYM, bid=100.0, ask=100.0)
    return ex, PortfolioTracker(ex, balance)


def test_missing_state_returns_false(tmp_path):
    store = PaperAccountStore(tmp_path / "acct.json", "t1")
    ex, tracker = world()
    assert store.restore_into(ex, tracker) is False


def test_crash_and_resume_preserves_account(tmp_path):
    store = PaperAccountStore(tmp_path / "acct.json", "t1")

    # session A: open a position, take a prior loss, mark a peak
    ex_a, tr_a = world()
    ex_a.submit_order(PaperOrderRequest("o1", SYM, True, 1.0))
    ex_a.set_quote(SYM, bid=95.0, ask=95.0)
    ex_a.submit_order(PaperOrderRequest("o2", SYM, False, 1.0, reduce_only=True))
    tr_a.on_bar(datetime(2026, 7, 3, 10, tzinfo=UTC))
    assert tr_a.consecutive_losses == 1
    ex_a.submit_order(PaperOrderRequest("o3", SYM, True, 2.0))  # open again
    store.save_from(ex_a, tr_a)

    # session B: fresh objects, restore
    ex_b, tr_b = world()
    assert store.restore_into(ex_b, tr_b) is True
    assert ex_b.balance_usd == pytest.approx(ex_a.balance_usd)
    pos = ex_b.get_positions()[0]
    assert pos.quantity == pytest.approx(2.0)
    assert pos.entry_price == pytest.approx(95.0)
    assert tr_b.consecutive_losses == 1
    assert tr_b.peak_equity_usd == pytest.approx(tr_a.peak_equity_usd)

    # closing the restored position keeps round-trip accounting coherent
    ex_b.set_quote(SYM, bid=90.0, ask=90.0)
    ex_b.submit_order(PaperOrderRequest("o4", SYM, False, 2.0, reduce_only=True))
    tr_b.on_bar(datetime(2026, 7, 3, 12, tzinfo=UTC))
    assert tr_b.consecutive_losses == 2  # restored streak + this losing close


def test_trial_id_mismatch_refused(tmp_path):
    store_a = PaperAccountStore(tmp_path / "acct.json", "trial_a")
    ex, tracker = world()
    store_a.save_from(ex, tracker)
    store_b = PaperAccountStore(tmp_path / "acct.json", "trial_b")
    with pytest.raises(ValueError, match="refusing to mix trials"):
        store_b.restore_into(ex, tracker)


def test_session_persists_each_bar(tmp_path):
    from tests.test_live_paper import AlwaysLong, FakeFeed, history, live_rows
    from vnedge.execution.journal import DecisionJournal
    from vnedge.execution.order_manager import OrderManager
    from vnedge.paper.paper_broker import PaperBroker
    from vnedge.risk.kill_switch import KillSwitch
    from vnedge.risk.risk_manager import PreTradeRiskGateway
    from vnedge.runtime.live_paper import LivePaperSession
    from vnedge.runtime.runner_config import RunnerConfig, RunnerMode

    config = RunnerConfig(mode=RunnerMode.PAPER, symbol=SYM, reconcile_every_bars=2)
    exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
    journal = DecisionJournal(tmp_path / "j.jsonl")
    gateway = PreTradeRiskGateway(config.risk, KillSwitch(kill_file=tmp_path / "K"))
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    store = PaperAccountStore(tmp_path / "acct.json", "t1")
    session = LivePaperSession(
        AlwaysLong(), FakeFeed(live_rows(n=1)), history(), config,
        gateway=gateway, order_manager=om, exchange=exchange, journal=journal,
        account_store=store,
    )
    asyncio.run(session.run(max_bars=1))
    state = store.load()
    assert state is not None
    assert len(state["positions"]) == 1  # the entry that just filled is persisted
