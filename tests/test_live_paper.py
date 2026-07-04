"""Live paper session — deterministic tests via a fake feed."""

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.data.schemas import normalize_candles
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_reconciliation import ReconciliationReport
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import PaperOrderRequest, SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import MarketState, PreTradeRiskGateway
from vnedge.runtime.live_paper import LivePaperSession
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

BASE = 1_750_000_000_000
MIN = 60_000
SYM = "BTC/USDT:USDT"


class FakeFeed:
    """Same surface as LiveMarketFeed, scripted content, no network."""

    exchange_id = "fake"

    def __init__(self, rows, quote=(99.99, 100.01), stale: bool = False):
        self.closed_candles = asyncio.Queue()
        for row in rows:
            self.closed_candles.put_nowait(row)
        self.quote = quote
        self.funding_rate = 0.0001
        self.stale = stale
        self.healthy = True

    def staleness_seconds(self, now=None):
        return 9_999.0 if self.stale else 0.5

    def market_state(self) -> MarketState:
        last = datetime.now(UTC) - (
            timedelta(hours=3) if self.stale else timedelta(milliseconds=100)
        )
        bid, ask = self.quote
        return MarketState(
            symbol=SYM, last_update=last,
            spread_bps=(ask - bid) / ((ask + bid) / 2) * 10_000,
            estimated_slippage_bps=2.0, funding_rate=self.funding_rate,
            exchange_healthy=self.healthy,
        )


class AlwaysLong(BaseStrategy):
    strategy_id = "always_long"
    warmup_bars = 2

    def prepare(self, candles):
        return candles.copy()

    def signal(self, df, index):
        close = float(df["close"].iloc[index])
        return SignalIntent("long", stop_price=close * 0.95,
                            take_profit_price=close * 1.10)


def history(n=5) -> pd.DataFrame:
    return normalize_candles(
        [[BASE + i * MIN, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(n)]
    )


def live_rows(start=5, n=3, low=99.5, high=100.5):
    return [[BASE + (start + i) * MIN, 100.0, high, low, 100.0, 5.0] for i in range(n)]


def build_session(tmp_path, feed, strategy=None, script=None, mode=RunnerMode.PAPER):
    config = RunnerConfig(mode=mode, symbol=SYM, reconcile_every_bars=2)
    exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
    journal = DecisionJournal(tmp_path / "journal.jsonl")
    kill = KillSwitch(kill_file=tmp_path / "KILL")
    gateway = PreTradeRiskGateway(config.risk, kill)
    om = OrderManager(gateway, journal, PaperBroker(exchange, script=script))
    session = LivePaperSession(
        strategy or AlwaysLong(), feed, history(), config,
        gateway=gateway, order_manager=om, exchange=exchange, journal=journal,
    )
    return session, exchange


async def test_closed_candle_triggers_full_pipeline(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed)
    report = await session.run(max_bars=1)
    assert report.bars_processed == 1
    assert report.signals_generated == 1
    assert report.orders_submitted == 1
    assert len(exchange.get_positions()) == 1  # filled at live quote
    fill = exchange.get_fills()[0]
    assert fill.price == pytest.approx(100.01 * (1 + 2 / 10_000))  # ask + slippage


async def test_stale_feed_blocks_entries(tmp_path):
    feed = FakeFeed(live_rows(n=1), stale=True)
    session, exchange = build_session(tmp_path, feed)
    report = await session.run(max_bars=1)
    assert report.signals_generated == 1
    assert report.risk_rejects == 1  # data_freshness failed at the gateway
    assert exchange.get_positions() == []


async def test_shadow_live_evaluates_and_journals_without_submission(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.SHADOW)

    report = await session.run(max_bars=1)

    assert report.mode == "shadow_live"
    # startup prime (latest seeded bar) + one forward bar = two shadow intents
    assert report.signals_generated == 2
    assert report.shadow_approved == 2
    assert report.orders_submitted == 0
    assert report.fills == 0
    assert exchange.get_positions() == []
    records = [r for r in session.journal.read_all() if r["kind"] == "shadow_intent"]
    assert len(records) == 2
    assert all(r["payload"]["approved"] is True for r in records)


async def test_shadow_prime_fires_on_startup_without_a_new_bar(tmp_path):
    # no new candles arrive; the seeded latest bar is armed (AlwaysLong).
    # a restart must NOT discard that — the startup prime journals it.
    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.SHADOW)

    report = await session.run(max_bars=0)

    records = [r for r in session.journal.read_all() if r["kind"] == "shadow_intent"]
    assert len(records) == 1          # primed off the seeded bar, zero new bars
    assert report.shadow_approved == 1
    assert report.orders_submitted == 0
    assert report.fills == 0
    assert exchange.get_positions() == []


async def test_paper_mode_is_not_primed_on_startup(tmp_path):
    # paper/live must never re-enter the latest bar on restart (double-position)
    feed = FakeFeed([])
    session, exchange = build_session(tmp_path, feed, mode=RunnerMode.PAPER)

    await session.run(max_bars=0)

    assert session.orders_submitted == 0      # nothing submitted from a prime
    assert exchange.get_positions() == []


async def test_non_forward_candles_dropped(tmp_path):
    stale_row = [[BASE + 2 * MIN, 100.0, 100.5, 99.5, 100.0, 5.0]]  # inside history
    feed = FakeFeed(stale_row + live_rows(n=1))
    session, exchange = build_session(tmp_path, feed)
    report = await session.run(max_bars=1)
    assert session.dropped_candles == 1
    assert report.bars_processed == 1  # only the valid candle counted


class LongOnce(AlwaysLong):
    strategy_id = "long_once"

    def __init__(self):
        self.fired = False

    def signal(self, df, index):
        if self.fired:
            return None
        self.fired = True
        return super().signal(df, index)


async def test_stop_exit_on_live_bar(tmp_path):
    # bar 1 opens position; bar 2's low pierces the 95 stop
    rows = live_rows(n=1) + [[BASE + 6 * MIN, 100.0, 100.2, 94.0, 96.0, 5.0]]
    feed = FakeFeed(rows)
    session, exchange = build_session(tmp_path, feed, strategy=LongOnce())
    report = await session.run(max_bars=2)
    assert exchange.get_positions() == []  # stopped out, flat
    assert report.fills == 2
    assert report.realized_pnl_usd < 0
    assert report.reconciliation_mismatches == 0


async def test_timeout_reached_entry_activates_plan_after_reconciliation(tmp_path):
    rows = live_rows(n=1) + [[BASE + 6 * MIN, 100.0, 100.2, 94.0, 96.0, 5.0]]
    feed = FakeFeed(rows)
    session, exchange = build_session(
        tmp_path, feed, strategy=LongOnce(), script=["timeout_reached"]
    )

    report = await session.run(max_bars=2)

    assert report.orders_submitted == 2  # timed-out entry + reduce-only stop
    assert report.fills == 2
    assert exchange.get_positions() == []
    assert report.reconciliation_mismatches == 0


async def test_restored_orphan_position_trips_kill_and_blocks_entries(tmp_path):
    feed = FakeFeed(live_rows(n=1))
    session, exchange = build_session(tmp_path, feed, strategy=AlwaysLong())
    exchange.set_quote(SYM, bid=99.99, ask=100.01)
    exchange.submit_order(PaperOrderRequest("restored", SYM, True, 1.0))
    existing_fills = len(exchange.get_fills())

    report = await session.run(max_bars=1)

    assert session.gateway.kill_switch.is_active
    assert report.risk_rejects == 1
    assert len(exchange.get_fills()) == existing_fills
    kinds = [r["kind"] for r in session.journal.read_all()]
    assert "orphaned_paper_position" in kinds


def test_reconciliation_mismatch_trips_live_session_fail_closed_once(tmp_path):
    feed = FakeFeed([])
    session, _ = build_session(tmp_path, feed)

    session.reconciler.run = lambda: ReconciliationReport((), ("internal vs venue",))
    session._reconcile()
    session._reconcile()

    assert session.gateway.kill_switch.is_active
    records = [
        r for r in session.journal.read_all()
        if r["kind"] == "reconciliation_fail_closed"
    ]
    assert len(records) == 1
    assert records[0]["payload"]["mismatches"] == ["internal vs venue"]
