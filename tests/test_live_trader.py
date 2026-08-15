"""Live trader runtime — three-gate enforcement + wiring via fakes (no keys)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.config.settings import LIVE_CONFIRMATION_PHRASE, Settings, TradingMode
from vnedge.data.schemas import normalize_candles
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.live_reconciliation import LiveReconciler
from vnedge.execution.order_manager import FlattenTarget, OrderManager
from vnedge.execution.private_stream import PrivateStreamHealth
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.position_sizer import SymbolLimits
from vnedge.risk.protections import ProtectionConfig, ProtectionState
from vnedge.risk.risk_manager import AccountState, MarketState, PreTradeRiskGateway
from vnedge.runtime.daily_factory import DailySignalFactoryConfig
from vnedge.runtime.pre_live_checklist import run_pre_live_checklist
from vnedge.runtime.live_trader import LiveTraderSession
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

BASE = 1_750_000_000_000
HOUR = 3_600_000
SYM = "BTC/USDT:USDT"
LIMITS = SymbolLimits(min_qty=0.0001, qty_step=0.0001, min_notional_usd=5.0,
                      maintenance_margin_rate=0.005)


def live_settings(mode=TradingMode.LIVE_SMALL, enabled=True,
                  phrase=LIVE_CONFIRMATION_PHRASE, **kw):
    kw.setdefault("live_small_capital_cap_usd", 100_000.0)
    return Settings(_env_file=None, trading_mode=mode, live_trading_enabled=enabled,
                    confirm_live_trading=phrase, risk=RiskConfig(), **kw)


class FakeFeed:
    exchange_id = "binanceusdm"

    def __init__(self, rows, quote=(99.99, 100.01)):
        self.closed_candles = asyncio.Queue()
        for r in rows:
            self.closed_candles.put_nowait(r)
        self.quote = quote

    def market_state(self):
        return MarketState(SYM, datetime.now(UTC) - timedelta(milliseconds=100),
                           spread_bps=2.0, estimated_slippage_bps=2.0,
                           funding_rate=0.0001, exchange_healthy=True)


class FakeLiveAdapter:
    """Implements the ExecutionAdapter + fetch_order_status surface."""

    def __init__(self, script=None):
        self.submitted = []
        self._script = list(script or [])
        self._status = {}

    async def submit_order(self, order):
        self.submitted.append(order.client_order_id)
        behavior = self._script.pop(0) if self._script else "ack"
        if behavior == "timeout":
            from vnedge.execution.order_manager import AdapterTimeout
            raise AdapterTimeout("no ack")
        if behavior == "reject":
            from vnedge.execution.order_manager import AdapterRejection
            raise AdapterRejection("venue rejected")
        if behavior == "timeout_reached":
            from vnedge.execution.order_manager import AdapterTimeout

            self._status[order.client_order_id] = {
                "status": "closed",
                "filled": order.intent.quantity,
            }
            raise AdapterTimeout("ack lost after venue accepted")
        self._status[order.client_order_id] = {"status": "closed", "filled": order.intent.quantity}
        return f"ex_{len(self.submitted)}"

    async def cancel_order(self, order):
        return "cancelled"

    async def fetch_order_status(self, order):
        return self._status.get(order.client_order_id)


class FakeAccounts:
    def __init__(self, equity=800.0, positions=None):
        self._equity = equity
        self._positions = positions or []

    async def account_state(self):
        return AccountState(equity_usd=self._equity, daily_pnl_usd=0.0,
                            peak_equity_usd=self._equity, open_positions=len(self._positions))

    async def open_positions(self):
        return list(self._positions)


def wire(settings, feed, adapter, accounts, tmp_path, strategy, **session_kw):
    journal = DecisionJournal(tmp_path / "j.jsonl")
    gateway = PreTradeRiskGateway(settings.risk, KillSwitch(kill_file=tmp_path / "K"))
    om = OrderManager(gateway, journal, adapter)
    reconciler = LiveReconciler(om, adapter)
    hist = normalize_candles([[BASE + i * HOUR, 100.0, 101.0, 99.0, 100.0, 10.0]
                              for i in range(5)])
    return LiveTraderSession(
        strategy, feed, hist, settings=settings, gateway=gateway, order_manager=om,
        reconciler=reconciler, account_provider=accounts, symbol=SYM, limits=LIMITS,
        **session_kw,
    ), om


class OneShotLong(BaseStrategy):
    strategy_id = "oneshot"
    warmup_bars = 2

    def __init__(self, at_bar=6):
        self.at_bar = at_bar
        self._fired = False

    def prepare(self, candles):
        return candles.copy()

    def signal(self, df, index):
        if self._fired or len(df) < self.at_bar:
            return None
        self._fired = True
        return SignalIntent("long", stop_price=95.0, take_profit_price=106.0)


# --- THE GATE ---------------------------------------------------------------------

@pytest.mark.parametrize("settings", [
    Settings(_env_file=None, trading_mode=TradingMode.PAPER),                       # not a live mode
    Settings(_env_file=None, trading_mode=TradingMode.LIVE_SMALL, live_trading_enabled=False),
    Settings(_env_file=None, trading_mode=TradingMode.LIVE_SMALL, live_trading_enabled=True,
             confirm_live_trading="wrong"),
])
def test_refuses_to_run_without_all_three_gates(settings, tmp_path):
    with pytest.raises(RuntimeError, match="three live gates"):
        wire(settings, FakeFeed([]), FakeLiveAdapter(), FakeAccounts(), tmp_path, OneShotLong())


def test_constructs_when_all_gates_open(tmp_path):
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(), tmp_path, OneShotLong())
    assert session.entries_allowed


def test_m3_live_refuses_disabled_daily_loss_halt(tmp_path):
    # A direct construction with a paper-only risk config (halt OFF) must be refused
    # for a live session even without a pre-live report wired.
    bad = Settings(_env_file=None, trading_mode=TradingMode.LIVE_SMALL,
                   live_trading_enabled=True, confirm_live_trading=LIVE_CONFIRMATION_PHRASE,
                   live_small_capital_cap_usd=100_000.0,
                   risk=RiskConfig(daily_loss_halt_enabled=False))
    with pytest.raises(RuntimeError, match="daily_loss_halt_enabled"):
        wire(bad, FakeFeed([]), FakeLiveAdapter(), FakeAccounts(), tmp_path, OneShotLong())


def test_m3_live_refuses_fixed_margin_sizing(tmp_path):
    bad = Settings(_env_file=None, trading_mode=TradingMode.LIVE_SMALL,
                   live_trading_enabled=True, confirm_live_trading=LIVE_CONFIRMATION_PHRASE,
                   live_small_capital_cap_usd=100_000.0,
                   risk=RiskConfig(fixed_margin_usd=10.0))
    with pytest.raises(RuntimeError, match="fixed_margin_usd"):
        wire(bad, FakeFeed([]), FakeLiveAdapter(), FakeAccounts(), tmp_path, OneShotLong())


def test_refuses_failed_pre_live_report(tmp_path):
    settings = live_settings()
    report = run_pre_live_checklist(
        settings=settings,
        risk_config=settings.risk,
        kill_switch_active=True,
        has_unresolved_orders=False,
        journal_path=tmp_path / "j.jsonl",
        credentials_present=True,
        lower_rungs_validated=True,
    )
    assert not report.cleared
    with pytest.raises(RuntimeError, match="pre-live checklist"):
        wire(
            settings,
            FakeFeed([]),
            FakeLiveAdapter(),
            FakeAccounts(),
            tmp_path,
            OneShotLong(),
            pre_live_report=report,
        )


# --- Wiring -----------------------------------------------------------------------

def bar(i, o=100.0, h=101.0, low=99.0, c=100.0):
    return [BASE + (5 + i) * HOUR, o, h, low, c, 10.0]


async def test_entry_flows_through_real_adapter(tmp_path):
    adapter = FakeLiveAdapter()
    feed = FakeFeed([bar(0)])
    session, om = wire(live_settings(), feed, adapter, FakeAccounts(), tmp_path,
                       OneShotLong(at_bar=6))
    await session.run(max_bars=1)
    assert session.signals == 1
    assert len(adapter.submitted) == 1  # real adapter received the order
    assert session.orders_submitted == 1


async def test_emergency_reduce_only_blocks_entries(tmp_path):
    session, om = wire(live_settings(mode=TradingMode.EMERGENCY_REDUCE_ONLY),
                       FakeFeed([bar(0)]), FakeLiveAdapter(), FakeAccounts(),
                       tmp_path, OneShotLong(at_bar=6))
    assert not session.entries_allowed
    await session.run(max_bars=1)
    assert session.orders_submitted == 0  # no entry in reduce-only mode


async def test_capital_cap_refuses_entry(tmp_path):
    adapter = FakeLiveAdapter()
    session, om = wire(live_settings(live_small_capital_cap_usd=100.0),
                       FakeFeed([bar(0)]), adapter, FakeAccounts(equity=500.0),
                       tmp_path, OneShotLong(at_bar=6))
    await session.run(max_bars=1)
    assert adapter.submitted == []  # equity over cap -> no order


async def test_required_private_stream_blocks_entries_when_stale(tmp_path):
    adapter = FakeLiveAdapter()
    health = PrivateStreamHealth(connected=False)
    session, om = wire(
        live_settings(),
        FakeFeed([bar(0)]),
        adapter,
        FakeAccounts(),
        tmp_path,
        OneShotLong(at_bar=6),
        require_private_stream=True,
        private_stream_health=health,
    )

    await session.run(max_bars=1)

    assert adapter.submitted == []
    assert session.private_stream_ready() is False


async def test_required_private_stream_allows_entries_when_fresh(tmp_path):
    adapter = FakeLiveAdapter()
    health = PrivateStreamHealth(connected=True, last_event_at=datetime.now(UTC))
    session, om = wire(
        live_settings(),
        FakeFeed([bar(0)]),
        adapter,
        FakeAccounts(),
        tmp_path,
        OneShotLong(at_bar=6),
        require_private_stream=True,
        private_stream_health=health,
    )

    await session.run(max_bars=1)

    assert len(adapter.submitted) == 1


async def test_emergency_flatten_submits_reduce_only(tmp_path):
    adapter = FakeLiveAdapter()
    accounts = FakeAccounts(positions=[FlattenTarget(SYM, "long", 0.01)])
    session, om = wire(live_settings(), FakeFeed([]), adapter, accounts, tmp_path,
                       OneShotLong())
    await session.emergency_flatten()
    assert len(adapter.submitted) == 1
    flat_order = next(iter(om.orders.values()))
    assert flat_order.intent.reduce_only is True


async def test_timeout_order_blocks_new_risk_until_reconciled(tmp_path):
    adapter = FakeLiveAdapter(script=["timeout"])
    feed = FakeFeed([bar(0)])
    session, om = wire(live_settings(), feed, adapter, FakeAccounts(), tmp_path,
                       OneShotLong(at_bar=6))
    await session.run(max_bars=1)
    # the entry timed out -> TIMEOUT_UNKNOWN; but fetch_order_status returns None
    # (never recorded), so reconciler resolves it to REJECTED
    assert not om.has_unresolved_orders  # reconciled at end of run


async def test_timeout_reached_entry_plan_survives_reconciliation(tmp_path):
    adapter = FakeLiveAdapter(script=["timeout_reached"])
    feed = FakeFeed([bar(0)])
    # the venue ACCEPTED the order (ack lost), so the account truly holds the
    # position — position reconciliation then AGREES with the adopted plan.
    accounts = FakeAccounts(positions=[FlattenTarget(SYM, "long", 0.01)])
    session, om = wire(live_settings(), feed, adapter, accounts, tmp_path,
                       OneShotLong(at_bar=6))

    await session.run(max_bars=1)

    assert session.orders_submitted == 1
    assert not om.has_unresolved_orders
    assert session._plan is not None
    assert session._reconciliation_halt is False   # venue agrees → no spurious halt


async def test_live_exit_plan_survives_reject_and_retries_with_new_key(tmp_path):
    adapter = FakeLiveAdapter(script=["reject", "ack"])
    accounts = FakeAccounts(positions=[FlattenTarget(SYM, "long", 0.01)])
    session, om = wire(
        live_settings(),
        FakeFeed([]),
        adapter,
        accounts,
        tmp_path,
        OneShotLong(),
    )
    session._plan = SignalIntent("long", stop_price=95.0, take_profit_price=106.0)
    session._entry_bar_ts = pd.Timestamp(BASE, unit="ms", tz="UTC")

    await session._submit_exit("stop", datetime.now(UTC))

    assert session._plan is not None
    assert session.orders_submitted == 1

    await session._submit_exit("stop", datetime.now(UTC))

    assert session._plan is None
    keys = [o.intent_key for o in om.orders.values() if o.intent.reduce_only]
    assert keys[-1] == keys[-2] + "|retry=1"


async def test_live_timeout_lost_exit_plan_waits_for_reconcile_before_retry(tmp_path):
    adapter = FakeLiveAdapter(script=["timeout", "ack"])
    accounts = FakeAccounts(positions=[FlattenTarget(SYM, "long", 0.01)])
    session, om = wire(
        live_settings(),
        FakeFeed([]),
        adapter,
        accounts,
        tmp_path,
        OneShotLong(),
    )
    session._plan = SignalIntent("long", stop_price=95.0, take_profit_price=106.0)
    session._entry_bar_ts = pd.Timestamp(BASE, unit="ms", tz="UTC")

    await session._submit_exit("stop", datetime.now(UTC))
    await session._submit_exit("stop", datetime.now(UTC))

    assert session._plan is not None
    assert session.orders_submitted == 1

    await session._reconcile()
    await session._submit_exit("stop", datetime.now(UTC))

    assert session._plan is None
    keys = [o.intent_key for o in om.orders.values() if o.intent.reduce_only]
    assert keys[-1] == keys[-2] + "|retry=1"


# --- Gap 2: position-level reconciliation fails closed -----------------------------

async def test_position_mismatch_fails_closed(tmp_path):
    # Flat internally, but the venue reports a position we don't track -> mismatch.
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(positions=[{"contracts": 1.0}]), tmp_path, OneShotLong())
    assert session.entries_allowed and session._plan is None  # clean + flat to start
    await session._reconcile_positions()
    assert session.recon_mismatches == 1
    assert session.entries_allowed is False  # failed closed: entries blocked, exits flow


async def test_position_reconciliation_clears_on_clean_pass(tmp_path):
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(positions=[{"contracts": 1.0}]), tmp_path, OneShotLong())
    await session._reconcile_positions()               # trip the halt
    assert session.entries_allowed is False
    session.accounts._positions = []                   # venue now flat — agrees with internal
    await session._reconcile_positions()               # clean settled pass
    assert session.entries_allowed is True
    assert session.recon_mismatches == 1               # the one real mismatch, not re-counted


async def test_position_recon_skips_while_orders_in_flight(tmp_path):
    # Unsettled state (an exit in flight) must NOT be judged against the venue.
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(positions=[{"contracts": 1.0}]), tmp_path, OneShotLong())
    session._pending_exit_orders["k"] = "coid"
    await session._reconcile_positions()
    assert session.recon_mismatches == 0 and session.entries_allowed is True


# --- L4: reconciliation rebuild-from-venue ---
async def test_l4_stale_plan_dropped_when_venue_flat(tmp_path):
    # We believe we hold, but the venue is flat (external close/liquidation).
    # Without the rebuild the halt is PERMANENT (plan never clears); with it,
    # the stale plan is dropped and entries auto-resume after a clean pass.
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(positions=[]), tmp_path, OneShotLong())
    sig = SignalIntent("long", stop_price=95.0, take_profit_price=106.0)
    session._plan = sig
    session._open_exit_state(sig, 0.01)
    await session._reconcile_positions()          # expected=True, actual=False → drop plan
    assert session.recon_mismatches == 1
    assert session._plan is None                  # rebuilt to flat (venue truth)
    assert session._reconciliation_halt is True
    await session._reconcile_positions()          # flat both sides → clean pass
    assert session._reconciliation_halt is False  # auto-resumed
    assert session.recon_mismatches == 1          # the one real mismatch, not re-counted


async def test_l4_orphan_position_flagged_and_journaled(tmp_path):
    # The venue holds a position we don't track: reduce-only + surfaced, no auto-adopt.
    session, om = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                       FakeAccounts(positions=[{"contracts": 1.0}]), tmp_path, OneShotLong())
    await session._reconcile_positions()
    assert session._orphan_position is True
    assert session.entries_allowed is False       # reduce-only until an operator flatten
    kinds = [r["kind"] for r in om._journal.read_all()]
    assert "reconciliation_rebuild" in kinds
    # once the orphan is gone, the latch (and the flag) clear on a clean pass
    session.accounts._positions = []
    await session._reconcile_positions()
    assert session._orphan_position is False and session.entries_allowed is True


# --- A2 audit fix: reconciliation fails closed on persistent account-read failure ---
async def test_reconcile_read_failure_fails_closed_after_n(tmp_path):
    class BoomAccounts(FakeAccounts):
        async def account_state(self):
            raise RuntimeError("account read down")

    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      BoomAccounts(), tmp_path, OneShotLong())
    assert session._reconciliation_halt is False
    for _ in range(session._MAX_RECON_READ_FAILURES):
        await session._reconcile_positions()
    assert session._recon_read_failures == session._MAX_RECON_READ_FAILURES
    assert session._reconciliation_halt is True          # fail closed: can't verify the venue


async def test_reconcile_clean_read_clears_failure_streak(tmp_path):
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(), tmp_path, OneShotLong())
    session._recon_read_failures = 2
    await session._reconcile_positions()                 # clean read, flat + agree
    assert session._recon_read_failures == 0
    assert session._reconciliation_halt is False


# --- A1: live exits go through the shared ActiveExitState engine ---
def test_a1_max_holding_hit_counts_bars(tmp_path):
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      FakeAccounts(), tmp_path, OneShotLong(), max_holding_bars=3)
    session._entry_bar_index = 0
    session._bars = 2
    assert session._max_holding_hit() is False
    session._bars = 3
    assert session._max_holding_hit() is True


async def test_a1_stop_exit_via_shared_engine_full_position(tmp_path):
    accounts = FakeAccounts(positions=[FlattenTarget(SYM, "long", 0.01)])
    session, om = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(), accounts,
                       tmp_path, OneShotLong())
    sig = SignalIntent("long", stop_price=95.0, take_profit_price=110.0)
    session._plan = sig
    session._open_exit_state(sig, 0.01)
    session.candles = normalize_candles([[BASE + i * HOUR, 100.0, 101.0, 99.0, 100.0, 10.0]
                                         for i in range(3)])
    # a bar whose LOW breaches the 95 stop → the shared engine returns a stop exit
    bar = pd.Series({"high": 101.0, "low": 94.0, "close": 96.0})
    await session._manage_exit(bar, __import__("datetime").datetime.now(__import__("datetime").UTC))
    assert session.orders_submitted >= 1 and session._plan is None   # full-position exit fired


async def test_a1_no_hit_holds_and_does_not_exit(tmp_path):
    accounts = FakeAccounts(positions=[FlattenTarget(SYM, "long", 0.01)])
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(), accounts,
                      tmp_path, OneShotLong())
    sig = SignalIntent("long", stop_price=95.0, take_profit_price=110.0)
    session._plan = sig
    session._open_exit_state(sig, 0.01)
    bar = pd.Series({"high": 101.0, "low": 99.0, "close": 100.0})   # no stop, no TP
    await session._manage_exit(bar, __import__("datetime").datetime.now(__import__("datetime").UTC))
    assert session._plan is sig and session.orders_submitted == 0   # still holding


def test_a1_trailing_tightens_stop(tmp_path):
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(), FakeAccounts(),
                      tmp_path, OneShotLong(), trail_atr_mult=2.0, trail_atr_window=2)
    sig = SignalIntent("long", stop_price=95.0, take_profit_price=110.0)
    session._open_exit_state(sig, 0.01)
    session._exit_state.seed_entry(entry_price=100.0, quantity=0.01)
    # tight candles → ATR≈1; trail 2×ATR behind the 108 favorable peak → stop ~106
    session.candles = normalize_candles([[BASE + i * HOUR, 100.0, 100.5, 99.5, 100.0, 10.0]
                                         for i in range(4)])
    session._exit_state._update_mfe(high=108.0, low=99.5)   # favorable extreme
    before = session._exit_state.current_stop
    session._exit_state.trail_stop(session._trail_atr())
    assert session._exit_state.current_stop > before        # ratcheted tighter (up for a long)


# --- L5 audit fix: submit-path account-read fault fails closed (no loop crash) ---
async def test_submit_entry_read_fault_fails_closed(tmp_path):
    class BoomAccounts(FakeAccounts):
        async def account_state(self):
            raise RuntimeError("account read down")

    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      BoomAccounts(), tmp_path, OneShotLong())
    before = session._recon_read_failures
    # must not raise out of the submit path
    await session._submit_entry(SignalIntent("long", stop_price=95.0, take_profit_price=110.0),
                                __import__("datetime").datetime.now(__import__("datetime").UTC))
    assert session._recon_read_failures == before + 1 and session.orders_submitted == 0


# --- L3: entry-hygiene gates (arm-gate / protections / daily-factory) ---
async def test_l3_no_gates_wired_entry_flows(tmp_path):
    adapter = FakeLiveAdapter()
    session, _ = wire(live_settings(), FakeFeed([bar(0)]), adapter, FakeAccounts(),
                      tmp_path, OneShotLong(at_bar=6))
    await session.run(max_bars=1)
    assert len(adapter.submitted) == 1 and session.entry_hygiene_blocks == 0


async def test_l3_protection_cooldown_blocks_entry(tmp_path):
    prot = ProtectionState(ProtectionConfig(cooldown_bars_after_stop=10))
    prot.on_exit("stop", 0)   # cooldown armed through bar 10
    adapter = FakeLiveAdapter()
    session, _ = wire(live_settings(), FakeFeed([bar(0)]), adapter, FakeAccounts(),
                      tmp_path, OneShotLong(at_bar=6), protections=prot)
    await session.run(max_bars=1)
    assert adapter.submitted == []                            # never reached the venue
    assert session.entry_hygiene_blocks == 1
    assert session._last_entry_block.startswith("protection:")


async def test_l3_stop_exit_arms_protection_cooldown(tmp_path):
    prot = ProtectionState(ProtectionConfig(cooldown_bars_after_stop=5))
    accounts = FakeAccounts(positions=[FlattenTarget(SYM, "long", 0.01)])
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(), accounts,
                      tmp_path, OneShotLong(), protections=prot)
    session._plan = SignalIntent("long", stop_price=95.0, take_profit_price=106.0)
    session._entry_bar_ts = pd.Timestamp(BASE, unit="ms", tz="UTC")
    session._bars = 3
    await session._submit_exit("stop", datetime.now(UTC))     # a live stop exit
    allowed, reason = prot.entries_allowed(4)                 # 4 < cooldown_until(3+5)
    assert allowed is False and "cooldown" in reason          # the exit armed the breaker


async def test_l3_daily_factory_max_entries_blocks_entry(tmp_path):
    cfg = DailySignalFactoryConfig(enabled=True, max_entries_per_day=2)
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(), FakeAccounts(),
                      tmp_path, OneShotLong(), daily_factory=cfg)
    now = datetime.now(UTC)
    session._roll_daily_factory(now)      # establish the session day
    session._factory_entries_today = 2    # already at the per-day cap
    block = await session._entry_hygiene_block(now, idx=5)
    assert block is not None and block.startswith("daily_factory:daily_factory_max_entries")


async def test_l3_arm_gate_blocks_entry_when_tm_degraded(tmp_path):
    class BrokenTM:
        def on_kline_update(self, *a, **k):
            raise RuntimeError("tm down")           # -> _feed_time_machine sets degraded
        def check_health(self, *a, **k):
            pass
        def health_of(self, *a, **k):
            return "ok"
        def age_ms(self, *a, **k):
            return 0
    adapter = FakeLiveAdapter()
    session, _ = wire(live_settings(), FakeFeed([bar(0)]), adapter, FakeAccounts(),
                      tmp_path, OneShotLong(at_bar=6), time_machine=BrokenTM(), timeframe="1h")
    await session.run(max_bars=1)
    assert adapter.submitted == []
    assert session.entry_hygiene_blocks == 1
    assert session._last_entry_block == "candle_path:tm_error"


async def test_l3_daily_factory_read_fault_fails_closed(tmp_path):
    class BoomAccounts(FakeAccounts):
        async def account_state(self):
            raise RuntimeError("account read down")
    cfg = DailySignalFactoryConfig(enabled=True, max_entries_per_day=5)
    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(), BoomAccounts(),
                      tmp_path, OneShotLong(), daily_factory=cfg)
    now = datetime.now(UTC)
    block = await session._entry_hygiene_block(now, idx=5)
    assert block == "account_read_failed"   # no account truth -> refuse the entry


# --- L1 increment 2: immutable hash-chained fill ledger on the live path ---
async def test_l1inc2_accepted_entry_is_chained_and_deduped(tmp_path):
    from vnedge.execution.fill_ledger import FillLedger, verify_chain
    ledger = FillLedger(tmp_path / "fills.jsonl")
    adapter = FakeLiveAdapter()
    session, _ = wire(live_settings(), FakeFeed([bar(0)]), adapter, FakeAccounts(),
                      tmp_path, OneShotLong(at_bar=6), fill_ledger=ledger)
    await session.run(max_bars=1)
    assert ledger.records == 1                       # the accepted entry was chained
    assert verify_chain(tmp_path / "fills.jsonl").ok  # tamper-evident chain intact
    rec = [__import__("json").loads(l) for l in (tmp_path / "fills.jsonl").read_text().splitlines()][0]
    assert rec["kind"] == "entry" and rec["side"] == "buy"
    assert rec["fee_usd"] is None                    # honest: not faked pending the fill stream
    session._ledger_sweep(datetime.now(UTC))         # a second sweep must NOT double-record
    assert ledger.records == 1
    assert session._report().fills == 1              # report reflects the ledger count


def test_l1inc2_ledger_resumes_chain_across_restart(tmp_path):
    from vnedge.execution.fill_ledger import FillLedger, verify_chain
    p = tmp_path / "fills.jsonl"
    FillLedger(p).append({"ts": "t", "symbol": SYM, "side": "buy", "quantity": 0.01})
    resumed = FillLedger(p)                           # reopen: continues, doesn't restart
    resumed.append({"ts": "t2", "symbol": SYM, "side": "sell", "quantity": 0.01})
    assert resumed.records == 2 and verify_chain(p).ok


# --- L1 increment 1: _report tracks real venue equity / peak-drawdown ---
async def test_l1_report_tracks_real_equity_and_drawdown(tmp_path):
    class MovingAccounts(FakeAccounts):
        _eqs = [800.0, 850.0, 810.0]

        def __init__(self):
            super().__init__()
            self._i = 0

        async def account_state(self):
            eq = self._eqs[min(self._i, len(self._eqs) - 1)]
            self._i += 1
            return AccountState(equity_usd=eq, daily_pnl_usd=0.0,
                                peak_equity_usd=eq, open_positions=0)

    session, _ = wire(live_settings(), FakeFeed([]), FakeLiveAdapter(),
                      MovingAccounts(), tmp_path, OneShotLong())
    for _ in range(3):                       # 800 (start+peak) -> 850 (peak) -> 810
        await session._read_account()
    rep = session._report()
    assert rep.final_equity_usd == 810.0                      # real current equity, not 0
    assert rep.realized_pnl_usd == 10.0                       # 810 - 800 starting
    assert abs(rep.max_drawdown_pct - (850 - 810) / 850 * 100) < 0.01   # ~4.7% from the 850 peak
