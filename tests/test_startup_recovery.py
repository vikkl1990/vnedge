"""Startup recovery must retain execution truth without monopolizing HTTP."""
import asyncio
import threading
from dataclasses import asdict
from types import SimpleNamespace

import pandas as pd
import pytest

from vnedge.execution.journal import DecisionJournal
from vnedge.paper.fill_model import FillModel
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import OrderIntent, PreTradeRiskGateway
from vnedge.runtime.multi_lane import _restore_paper_session
from vnedge.runtime.runner_config import RunnerConfig
from vnedge.strategy.measurement_only import MeasurementOnly


def recovery_args(tmp_path):
    config = RunnerConfig()
    return dict(
        strategy=MeasurementOnly(), feed=SimpleNamespace(exchange_id="binanceusdm"),
        history=pd.DataFrame(), config=config,
        gateway=PreTradeRiskGateway(config.risk, KillSwitch(kill_file=tmp_path / "KILL")),
        exchange=SimulatedExchange(FillModel()), journal=DecisionJournal(tmp_path / "lane.jsonl"),
    )


async def test_recovery_yields_and_preserves_order_funding_and_evaluation_truth(tmp_path, monkeypatch):
    args = recovery_args(tmp_path)
    journal = args["journal"]
    intent = OrderIntent(symbol="BTC/USDT:USDT", side="long", quantity=.001,
                         notional_usd=100., leverage=3.)
    journal.append("order_intent", {"intent_key": "original-key", "client_order_id": "original-id",
                                    "intent": asdict(intent)})
    journal.append("funding_applied", {"symbol": "BTC/USDT:USDT", "book": "paper",
                                       "funding_event_id": "settlement-1"})
    journal.append("lane_eval", {"strategy_id": "measurement_only_v1", "symbol": "BTC/USDT:USDT",
                                "timeframe": "1h", "bar_ts": "2026-09-17T00:00:00+00:00",
                                "backfill": True})
    entered, release = threading.Event(), threading.Event()
    original_read = journal.read_all
    reader_threads = []

    def slow_read():
        reader_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3), "event loop could not release startup reader"
        return original_read()

    monkeypatch.setattr(journal, "read_all", slow_read)
    task = asyncio.create_task(_restore_paper_session(**args))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # This callback must run while the WAL reader is still blocked.
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    session = await asyncio.wait_for(task, 3)
    assert reader_threads and threading.get_ident() not in reader_threads
    assert len(reader_threads) == 3  # two OM projections + one shared session read
    assert session.om.has_unresolved_orders
    assert session.om._registry.existing_order_id("original-key") == "original-id"
    assert "original-id" in session.om.orders
    assert ("paper", "settlement-1") in session._funding_dispatch_keys
    assert ("measurement_only_v1", "BTC/USDT:USDT", "1h",
            "2026-09-17T00:00:00+00:00") in session._backfill_eval_keys
    assert args["exchange"].get_fills() == []
    assert len(original_read()) == 3  # construction is not an order submission


async def test_unreadable_recovery_never_returns_a_ready_session(tmp_path, monkeypatch):
    args = recovery_args(tmp_path)

    def fail_read():
        raise ValueError("corrupt historical record")

    monkeypatch.setattr(args["journal"], "read_all", fail_read)
    with pytest.raises(ValueError, match="corrupt historical"):
        await _restore_paper_session(**args)
    assert args["exchange"].get_fills() == []
