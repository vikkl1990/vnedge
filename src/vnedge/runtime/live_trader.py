"""Live trader runtime.

The paper session's counterpart that submits REAL orders through the
CcxtExecutionAdapter. Every safety property is preserved and one is added:

- THREE-GATE ENFORCEMENT: the session refuses to construct unless
  settings.is_live (a live_* mode AND live_trading_enabled AND the exact
  confirmation phrase). There is no way to run it with a real adapter
  without all three. The adapter adds ``live_confirmed`` as a fourth
  mainnet-construction gate; testnet/sandbox execution is not a validation
  path for scalping.
- No bypass of PreTradeRiskGateway: every intent goes through OrderManager,
  exactly as in paper.
- emergency_reduce_only mode blocks new entries; only reduce-only exits flow.
- capital cap: entries are refused once account equity reaches the
  live_small ceiling (defence in depth on top of the gateway's exposure caps).
- Unknown orders are resolved by LiveReconciler against venue truth, never
  by assumption; while any is unresolved, new risk is blocked.

Account state (equity, positions) comes from the venue via an
AccountProvider, so the gateway sees real balances. Built to be exercised
with fakes now (no keys) and wired to a live CcxtAccountProvider later.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

import pandas as pd

from vnedge.config.settings import LIVE_CONFIRMATION_PHRASE, Settings, TradingMode
from vnedge.data.time_machine import TimeMachine
from vnedge.execution.fill_ledger import FillLedger
from vnedge.execution.live_reconciliation import LiveReconciler
from vnedge.execution.order_manager import FlattenTarget, OrderManager
from vnedge.execution.order_state import OrderState
from vnedge.risk.position_sizer import SymbolLimits, size_position
from vnedge.risk.protections import ProtectionState
from vnedge.risk.risk_manager import AccountState, OrderIntent
from vnedge.runtime import latency_thresholds as LT
from vnedge.runtime.active_exit import ActiveExitState
from vnedge.runtime.daily_factory import (
    DailySignalFactoryConfig,
    entry_block_reason,
    session_day,
)
from vnedge.runtime.run_report import RunReport
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr as _atr_indicator

logger = logging.getLogger(__name__)

_EXIT_ACCEPTED_STATES = frozenset(
    {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}
)
_EXIT_RETRYABLE_STATES = frozenset(
    {OrderState.RISK_REJECTED, OrderState.REJECTED, OrderState.CANCELLED}
)


class AccountProvider(Protocol):
    """Supplies live account truth to the gateway."""

    async def account_state(self) -> AccountState: ...
    async def open_positions(self) -> list[FlattenTarget]: ...


class PrivateStreamHealthProvider(Protocol):
    connected: bool

    def age_seconds(self, now: datetime | None = None) -> float: ...


class LiveTraderSession:
    def __init__(
        self,
        strategy: BaseStrategy,
        feed,  # LiveMarketFeed or a fake with the same surface
        history: pd.DataFrame,
        *,
        settings: Settings,
        gateway,  # PreTradeRiskGateway
        order_manager: OrderManager,
        reconciler: LiveReconciler,
        account_provider: AccountProvider,
        symbol: str,
        limits: SymbolLimits,
        reconcile_every_bars: int = 1,
        pre_live_report=None,
        private_stream_health: PrivateStreamHealthProvider | None = None,
        require_private_stream: bool = False,
        max_private_stream_age_seconds: float = 5.0,
        max_holding_bars: int = 48,
        trail_atr_mult: float = 0.0,
        trail_atr_window: int = 14,
        timeframe: str = "1m",
        time_machine: TimeMachine | None = None,
        protections: ProtectionState | None = None,
        daily_factory: DailySignalFactoryConfig | None = None,
        fill_ledger: FillLedger | None = None,
    ) -> None:
        # --- THE GATE: no live trader without all three live gates open ---
        if not settings.is_live:
            raise RuntimeError(
                "LiveTraderSession requires all three live gates: a live_* mode, "
                "live_trading_enabled=true, and the exact confirmation phrase. "
                f"Current: mode={settings.trading_mode.value}, "
                f"enabled={settings.live_trading_enabled}, "
                f"phrase_ok={settings.confirm_live_trading == LIVE_CONFIRMATION_PHRASE}"
            )
        # M3 (defense in depth): the two paper-only risk knobs must never reach a
        # live session even by DIRECT construction (bypassing the entrypoint's
        # checklist) — a disabled daily-loss halt or leverage-based fixed-margin
        # sizing on real orders is exactly what the charter forbids.
        _rc = settings.risk
        if getattr(_rc, "daily_loss_halt_enabled", True) is not True:
            raise RuntimeError(
                "live session requires daily_loss_halt_enabled=True — the halt is "
                "paper-only-disableable and must be ON for live"
            )
        if getattr(_rc, "fixed_margin_usd", None) is not None:
            raise RuntimeError(
                "live session requires fixed_margin_usd=None — leverage-based sizing "
                "is paper-only; live sizes from risk-per-trade and stop distance"
            )
        if pre_live_report is not None and not pre_live_report.cleared:
            failures = ", ".join(f.name for f in pre_live_report.failures)
            raise RuntimeError(f"pre-live checklist not cleared: {failures}")
        if require_private_stream and private_stream_health is None:
            raise RuntimeError("require_private_stream=True needs private_stream_health")
        self.strategy = strategy
        self.feed = feed
        self.candles = history.reset_index(drop=True)
        self.settings = settings
        self.gateway = gateway
        self.om = order_manager
        self.reconciler = reconciler
        self.accounts = account_provider
        self.symbol = symbol
        self.limits = limits
        self.reconcile_every_bars = reconcile_every_bars
        self.private_stream_health = private_stream_health
        self.require_private_stream = require_private_stream
        self.max_private_stream_age_seconds = max_private_stream_age_seconds
        self.signals = self.orders_submitted = self.risk_rejects = 0
        self.sizing_skips = self.recon_mismatches = self.bar_faults = 0
        self._plan: SignalIntent | None = None
        self._entry_bar_ts = None
        self._parked_entries = {}
        self._pending_exit_orders: dict[str, str] = {}
        self._exit_retry_attempts: dict[str, int] = {}
        self._bars = 0
        # A1: real live exits go through the SAME ActiveExitState engine that
        # paper/shadow/backtest validate — trailing stop, breakeven arm, TP, and
        # the max_holding time cap — instead of a bare bar-close stop/TP. Exits
        # stay full-position (venue-is-truth), but the DECISION is the shared one.
        self._max_holding_bars = max_holding_bars
        self._trail_atr_mult = trail_atr_mult
        self._trail_atr_window = trail_atr_window
        self._exit_state: ActiveExitState | None = None
        self._position_qty = 0.0        # tracked entry size (resolve_bar gate)
        self._entry_bar_index: int | None = None
        # L1 settlement: real equity/peak/drawdown from the venue (account_state is
        # truth), tracked every account read, so _report() and the snapshot report
        # real capital instead of zeros. (Fill-level accounting = the fill ledger,
        # increment 2.)
        self._last_equity_usd = 0.0
        self._starting_equity_usd = 0.0
        self._peak_equity_usd = 0.0
        self._max_drawdown_pct = 0.0
        # L1 increment 2: immutable, hash-chained execution record. The live path
        # has no simulated exchange to read fills from, so we sweep the OM's
        # accepted/filled orders into the SAME resume-aware FillLedger paper uses
        # (deduped by client_order_id). Per-fill fee/realized_pnl await the private
        # fill stream (increment 3) — recorded null now, never faked.
        self.fill_ledger = fill_ledger
        self._ledgered_orders: set[str] = set()
        # Runtime fail-closed latch: set when position reconciliation finds the
        # venue diverging from internal state; blocks new entries (exits still
        # flow) until a clean settled pass clears it. The static
        # EMERGENCY_REDUCE_ONLY mode can't be flipped at runtime, so a live
        # divergence needs this latch to actually halt entries.
        self._reconciliation_halt = False
        # A persistently failing account-read disables divergence detection while
        # entries keep flowing; fail closed after this many consecutive failures.
        self._recon_read_failures = 0
        # L4: an untracked venue position (orphan) discovered by settled
        # reconciliation — reduce-only until an operator flatten clears it.
        self._orphan_position = False
        # L3 entry-hygiene gates: the SAME three checks paper runs before an entry
        # — the candle-path arm-gate (Time Machine health/age), the post-stop
        # protections breaker, and the daily-factory session/cap windows. All are
        # optional (injected); when unwired the gate is a no-op, so the gateway
        # remains the floor and existing construction is unchanged. Exits never
        # consult these (reduce-only always flows).
        self.timeframe = timeframe
        self.time_machine = time_machine
        self.protections = protections
        self.daily_factory = daily_factory
        self._tm_degraded = False
        self._factory_day = None
        self._factory_entries_today = 0
        self.entry_hygiene_blocks = 0
        self._last_entry_block: str | None = None
        self._entry_block_counts: dict[str, int] = {}

    _MAX_RECON_READ_FAILURES = 3

    @property
    def entries_allowed(self) -> bool:
        """emergency_reduce_only mode — or a reconciliation halt — allows exits only."""
        return (
            self.settings.trading_mode is not TradingMode.EMERGENCY_REDUCE_ONLY
            and not self._reconciliation_halt
        )

    def private_stream_ready(self, now: datetime | None = None) -> bool:
        if not self.require_private_stream:
            return True
        health = self.private_stream_health
        if health is None or not health.connected:
            return False
        return health.age_seconds(now) <= self.max_private_stream_age_seconds

    def _track_equity(self, account) -> None:
        """L1: maintain real equity / peak / max-drawdown from the venue truth."""
        eq = float(getattr(account, "equity_usd", 0.0) or 0.0)
        if eq <= 0:
            return
        self._last_equity_usd = eq
        if self._starting_equity_usd <= 0:
            self._starting_equity_usd = eq
        self._peak_equity_usd = max(self._peak_equity_usd, eq)
        if self._peak_equity_usd > 0:
            dd = (self._peak_equity_usd - eq) / self._peak_equity_usd * 100.0
            self._max_drawdown_pct = max(self._max_drawdown_pct, dd)

    async def _read_account(self):
        """Guarded account read on the SUBMIT path — a fault fail-closes (counts
        toward the reconciliation read-failure halt) instead of crashing run()."""
        try:
            st = await self.accounts.account_state()
        except Exception as exc:  # noqa: BLE001 — a read fault must not crash the loop
            self._note_submit_read_failure("account", exc)
            return None
        self._recon_read_failures = 0
        self._track_equity(st)
        return st

    async def _read_positions(self):
        try:
            return await self.accounts.open_positions()
        except Exception as exc:  # noqa: BLE001 — a read fault must not crash the loop
            self._note_submit_read_failure("positions", exc)
            return None

    def _note_submit_read_failure(self, kind: str, exc: Exception) -> None:
        self._recon_read_failures += 1
        logger.error("submit-path %s read failed (%d consecutive): %s",
                     kind, self._recon_read_failures, exc)
        if self._recon_read_failures >= self._MAX_RECON_READ_FAILURES:
            self._reconciliation_halt = True

    # --- L3: entry-hygiene gates (parity with paper's pre-entry discipline) ------
    def _tm_kline(self, row: list, now: datetime) -> dict:
        ts = pd.to_datetime(int(row[0]), unit="ms", utc=True).to_pydatetime()
        return {"open_time": ts, "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
                "exchange_ts": now}

    def _feed_time_machine(self, now: datetime, closed_row: list | None = None) -> None:
        """Feed the arm-gate's Time Machine. FAIL-SAFE: never raises into the loop;
        a fault only marks it degraded (which the arm-gate then reads as a block)."""
        if self.time_machine is None:
            return
        try:
            sym, tf = self.symbol, self.timeframe
            if closed_row is not None:
                self.time_machine.on_kline_update(sym, tf, self._tm_kline(closed_row, now), is_closed=True)
            forming = getattr(self.feed, "forming_candle", None)
            if forming:
                self.time_machine.on_kline_update(sym, tf, self._tm_kline(forming, now), is_closed=False)
            self.time_machine.check_health(now)
            self._tm_degraded = False
        except Exception as exc:  # noqa: BLE001 — the arm-gate must never break the loop
            self._tm_degraded = True
            logger.warning("time machine update failed: %s", exc)

    def _candle_path_arm_block(self, now: datetime) -> str | None:
        """Block a NEW entry (never an exit) when the decision timeframe's candle
        path is unsafe to arm on — health != ok, last-update age past the shared
        HARD budget, or a Time Machine fault. Same logic paper enforces."""
        tm = self.time_machine
        if tm is None:
            return None
        try:
            if self._tm_degraded:
                return "tm_error"
            tf = self.timeframe
            health = tm.health_of(self.symbol, tf)
            if health != "ok":
                return f"decision_tf_{health}"
            hard = LT.TM_AGE_HARD_LAST_MS.get(tf)
            age = tm.age_ms(self.symbol, tf, now)
            if hard is not None and age is not None and age > hard:
                return "tm_age_hard"
        except Exception:  # noqa: BLE001 — an arm-gate fault must not wedge the lane
            return None
        return None

    def _roll_daily_factory(self, clock: datetime) -> None:
        if self.daily_factory is None or not self.daily_factory.enabled:
            return
        day = session_day(clock, self.daily_factory)
        if day == self._factory_day:
            return
        self._factory_day = day
        self._factory_entries_today = 0

    def _daily_factory_entry_block_reason(self, now: datetime, account) -> str | None:
        self._roll_daily_factory(now)
        return entry_block_reason(
            now=now, config=self.daily_factory,
            entries_today=self._factory_entries_today,
            daily_pnl_usd=float(getattr(account, "daily_pnl_usd", 0.0) or 0.0),
        )

    async def _entry_hygiene_block(self, now: datetime, idx: int) -> str | None:
        """The three pre-entry gates, cheapest first. Returns a block reason or
        None. Fail-closed: if the daily-factory account read faults, refuse."""
        cp = self._candle_path_arm_block(now)
        if cp is not None:
            return f"candle_path:{cp}"
        if self.protections is not None:
            allowed, reason = self.protections.entries_allowed(idx)
            if not allowed:
                return f"protection:{reason}"
        if self.daily_factory is not None and self.daily_factory.enabled:
            account = await self._read_account()
            if account is None:
                return "account_read_failed"   # no entry without account truth
            fb = self._daily_factory_entry_block_reason(now, account)
            if fb is not None:
                return f"daily_factory:{fb}"
        return None

    def _note_entry_block(self, block: str) -> None:
        self.entry_hygiene_blocks += 1
        self._last_entry_block = block
        self._entry_block_counts[block] = self._entry_block_counts.get(block, 0) + 1
        logger.info("live entry blocked by hygiene gate: %s", block)

    async def _submit_entry(self, sig: SignalIntent, now: datetime) -> None:
        account = await self._read_account()
        if account is None:
            return  # fail-closed: skip this entry, do not crash the loop
        if account.equity_usd >= self.settings.live_small_capital_cap_usd \
                and self.settings.trading_mode is TradingMode.LIVE_SMALL:
            logger.warning("equity $%.2f at/above live_small cap $%.2f — entry refused",
                           account.equity_usd, self.settings.live_small_capital_cap_usd)
            return
        bid, ask = self.feed.quote
        ref = ask if sig.side == "long" else bid
        sizing = size_position(
            equity_usd=account.equity_usd, entry_price=ref, stop_price=sig.stop_price,
            side=sig.side, config=self.settings.risk, limits=self.limits,
        )
        if not sizing.approved:
            self.sizing_skips += 1
            return
        intent = OrderIntent(
            symbol=self.symbol, side=sig.side, quantity=sizing.quantity,
            notional_usd=sizing.notional_usd,
            leverage=max(sizing.required_leverage, 1.0),
            reduce_only=False, strategy_id=self.strategy.strategy_id,
        )
        from vnedge.execution.idempotency import make_intent_key

        key = make_intent_key(self.strategy.strategy_id, self.symbol, sig.side,
                              self.candles["timestamp"].iloc[-1])
        order = await self.om.submit(intent, account, self.feed.market_state(), key, now=now)
        if order.state is OrderState.RISK_REJECTED:
            self.risk_rejects += 1
        elif order.state is OrderState.ACKNOWLEDGED:
            self.orders_submitted += 1
            self._factory_entries_today += 1
            self._plan = sig
            self._entry_bar_ts = self.candles["timestamp"].iloc[-1]
            self._open_exit_state(sig, sizing.quantity)
        elif order.state is OrderState.TIMEOUT_UNKNOWN:
            self.orders_submitted += 1
            self._factory_entries_today += 1
            self._parked_entries[order.client_order_id] = (
                sig,
                self.candles["timestamp"].iloc[-1],
                sizing.quantity,
            )

    async def _submit_exit(self, reason: str, now: datetime) -> None:
        positions = await self._read_positions()
        if positions is None:
            return  # read fault → retry next bar; the exit plan persists (never dropped)
        pos = next((p for p in positions if p.symbol == self.symbol), None)
        if pos is None:
            self._clear_exit_plan()
            return
        intent = OrderIntent(
            symbol=self.symbol, side="short" if pos.side == "long" else "long",
            quantity=pos.quantity, notional_usd=0.0, leverage=1.0,
            reduce_only=True, strategy_id=self.strategy.strategy_id,
        )
        account = await self._read_account()
        if account is None:
            return  # read fault → retry next bar; the exit plan persists
        key_ts = (
            int(pd.Timestamp(self._entry_bar_ts).value)
            if self._entry_bar_ts is not None
            else int(now.timestamp() * 1000)
        )
        base_key = f"exit|{self.symbol}|{reason}|{key_ts}"
        pending = self._pending_exit_orders.get(base_key)
        if pending is not None:
            pending_order = self.om.orders.get(pending)
            if pending_order is not None and pending_order.state in (
                OrderState.TIMEOUT_UNKNOWN,
                OrderState.RECONCILING,
            ):
                return
            self._pending_exit_orders.pop(base_key, None)
        order = await self.om.submit(
            intent, account, self.feed.market_state(),
            intent_key=self._exit_intent_key(base_key), now=now,
        )
        self.orders_submitted += 1
        if order.state in _EXIT_ACCEPTED_STATES:
            if self.protections is not None:
                self.protections.on_exit(reason, self._bars)   # arm post-stop breaker
            self._clear_exit_plan()
        else:
            self._preserve_exit_plan(base_key, order)

    def _exit_intent_key(self, base_key: str) -> str:
        attempt = self._exit_retry_attempts.get(base_key, 0)
        return base_key if attempt == 0 else f"{base_key}|retry={attempt}"

    def _clear_exit_plan(self) -> None:
        self._plan = None
        self._entry_bar_ts = None
        self._exit_state = None
        self._position_qty = 0.0
        self._entry_bar_index = None
        self._pending_exit_orders.clear()
        self._exit_retry_attempts.clear()

    # --- A1: shared exit engine (same ActiveExitState as paper/shadow/backtest) ---
    def _open_exit_state(self, sig: SignalIntent, quantity: float) -> None:
        self._exit_state = ActiveExitState.from_signal(sig, trail_atr_mult=self._trail_atr_mult)
        self._position_qty = abs(float(quantity or 0.0))
        self._entry_bar_index = self._bars

    def _trail_atr(self) -> float:
        """Canonical ATR for the trail — the SAME indicator + window paper/shadow/
        backtest use, so the live trail matches what was validated. 0.0 if off/cold."""
        if self._trail_atr_mult <= 0.0 or len(self.candles) < 2:
            return 0.0
        try:
            v = float(_atr_indicator(self.candles, self._trail_atr_window).iloc[-1])
        except Exception:  # noqa: BLE001 — trailing must never break the exit loop
            return 0.0
        return v if v == v else 0.0

    def _max_holding_hit(self) -> bool:
        cap = self._max_holding_bars
        if cap <= 0 or self._entry_bar_index is None:
            return False
        return (self._bars - self._entry_bar_index) >= cap

    async def _manage_exit(self, bar: pd.Series, now: datetime) -> None:
        """Route the live exit DECISION through the shared ActiveExitState (stop,
        breakeven, TP, trailing, max_holding). Submit is full-position (venue is
        truth); any decision closes the whole position."""
        if self._plan is None or self._exit_state is None:
            return
        # seed the entry price lazily from the venue position (needed for
        # breakeven/ladder; the stop itself works without it).
        if self._exit_state.entry_price is None:
            try:
                positions = await self.accounts.open_positions()
                pos = next((p for p in positions if p.symbol == self.symbol), None)
                if pos is not None:
                    self._exit_state.seed_entry(entry_price=getattr(pos, "entry_price", None),
                                                quantity=pos.quantity)
                    if self._position_qty <= 0:
                        self._position_qty = abs(float(pos.quantity))
            except Exception as exc:  # noqa: BLE001 — a read fault must not break exits
                logger.warning("exit-state seed read failed: %s", exc)
        decision = self._exit_state.resolve_bar(
            high=float(bar["high"]), low=float(bar["low"]), close=float(bar["close"]),
            position_quantity=self._position_qty,
            min_qty=self.limits.min_qty, qty_step=self.limits.qty_step,
            max_holding_hit=self._max_holding_hit(),
        )
        if decision is None:
            self._exit_state.trail_stop(self._trail_atr())   # tighten for later bars
            return
        await self._submit_exit(decision.reason, now)         # full-position close

    def _preserve_exit_plan(self, base_key: str, order) -> None:
        if order.state in (OrderState.TIMEOUT_UNKNOWN, OrderState.RECONCILING):
            self._pending_exit_orders[base_key] = order.client_order_id
        elif order.state in _EXIT_RETRYABLE_STATES:
            self._exit_retry_attempts[base_key] = self._exit_retry_attempts.get(base_key, 0) + 1
        if order.state is OrderState.RISK_REJECTED:
            self.risk_rejects += 1
        logger.warning(
            "preserving live exit plan after submit ended %s (%s)",
            order.state.value,
            order.client_order_id,
        )

    async def emergency_flatten(self) -> None:
        """Close every venue position reduce-only through the normal pipeline."""
        positions = await self.accounts.open_positions()
        account = await self.accounts.account_state()
        self._track_equity(account)
        markets = {self.symbol: self.feed.market_state()}
        fid = f"flatten|{int(datetime.now(UTC).timestamp() * 1000)}"
        await self.om.emergency_flatten(positions, account, markets, fid,
                                        now=datetime.now(UTC))
        self._ledger_sweep(datetime.now(UTC))   # chain the flatten executions

    async def run(self, *, max_bars: int | None = None) -> RunReport:
        import asyncio

        while max_bars is None or self._bars < max_bars:
            try:
                raw = await asyncio.wait_for(self.feed.closed_candles.get(), timeout=5.0)
            except asyncio.TimeoutError:
                await self._reconcile()
                continue
            now = datetime.now(UTC)
            # M1: contain any bar-level fault — an unmapped submit error, a bad raw
            # candle, a strategy exception — to THIS bar instead of terminating the
            # real-money loop. The bar is skipped and a reconcile forced (a fault
            # mid-submit could have left an order in flight; new risk stays gated
            # until it resolves). Exit plans persist regardless (never dropped here).
            try:
                ts = pd.to_datetime(raw[0], unit="ms", utc=True)
                if len(self.candles) and ts <= self.candles["timestamp"].iloc[-1]:
                    continue
                row = {"timestamp": ts, "open": float(raw[1]), "high": float(raw[2]),
                       "low": float(raw[3]), "close": float(raw[4]), "volume": float(raw[5])}
                self.candles = pd.concat([self.candles, pd.DataFrame([row])], ignore_index=True)
                self._bars += 1
                self._feed_time_machine(now, raw)   # arm-gate input (fail-safe)

                bar = self.candles.iloc[-1]
                # exits first (always allowed, even in emergency_reduce_only) — the
                # shared exit engine: stop/breakeven/TP/trailing/max_holding.
                await self._manage_exit(bar, now)

                # entries only when allowed, flat, and nothing unresolved
                if (self.entries_allowed and self._plan is None
                        and not self.om.has_unresolved_orders
                        and self.private_stream_ready(now)
                        and len(self.candles) > self.strategy.warmup_bars):
                    df = self.strategy.prepare(self.candles)
                    idx = len(df) - 1
                    block = await self._entry_hygiene_block(now, idx)
                    if block is not None:
                        self._note_entry_block(block)   # arm-gate / protections / factory
                    else:
                        sig = self.strategy.signal(df, idx)
                        if sig is not None:
                            self.signals += 1
                            await self._submit_entry(sig, now)

                if self._bars % self.reconcile_every_bars == 0 or self.om.has_unresolved_orders:
                    await self._reconcile()

                self._ledger_sweep(now)   # chain any newly-accepted executions
            except Exception as exc:  # noqa: BLE001 — a bar fault must not crash the live loop
                self.bar_faults += 1
                logger.error("live bar processing fault (bar %d) — skipping bar: %s",
                             self._bars, exc)
                await self._reconcile()   # resolve any in-flight order; keep risk gated

        await self._reconcile()
        self._ledger_sweep(datetime.now(UTC))
        return self._report()

    async def _reconcile(self) -> None:
        try:
            resolved = await self.reconciler.resolve_unknown_orders()
        except Exception as exc:  # noqa: BLE001 — reconciliation errors must not crash the loop
            logger.error("live reconciliation failed: %s", exc)
            return
        for coid in resolved:
            parked = self._parked_entries.pop(coid, None)
            if parked is None:
                self._resolve_pending_exit(coid)
                continue
            order = self.om.orders[coid]
            if order.state in _EXIT_ACCEPTED_STATES and self._plan is None:
                sig, self._entry_bar_ts, qty = parked
                self._plan = sig
                self._open_exit_state(sig, qty)
        await self._reconcile_positions()

    async def _reconcile_positions(self) -> None:
        """Position-level reconciliation: the venue is truth. Only runs in a
        SETTLED state (no orders in flight), so transient order-flight gaps never
        false-trigger. A persistent divergence — a venue position we don't track,
        or a position we believe we hold that's vanished (external liquidation) —
        counts a mismatch and FAILS CLOSED (reduce-only) until a clean pass
        clears it. This is the halt the mismatch counter was reported for but
        nothing ever triggered."""
        if (self.om.has_unresolved_orders or self._parked_entries
                or self._pending_exit_orders):
            return  # in flight — not a settled state to judge the venue against
        try:
            account = await self.accounts.account_state()
        except Exception as exc:  # noqa: BLE001 — a failed read must not crash the loop
            self._recon_read_failures += 1
            logger.error("position reconciliation read failed (%d consecutive): %s",
                         self._recon_read_failures, exc)
            if self._recon_read_failures >= self._MAX_RECON_READ_FAILURES:
                # can't verify the venue is truth → fail closed, like a mismatch
                self._reconciliation_halt = True
                logger.error(
                    "reconciliation read failed %d× — FAIL CLOSED (reduce-only) "
                    "until a clean account read", self._recon_read_failures)
            return
        self._recon_read_failures = 0          # clean read → clear the failure streak
        self._track_equity(account)            # L1: real equity/peak/drawdown from venue truth
        expected = self._plan is not None          # we believe we hold a position
        actual = account.open_positions > 0        # the venue's truth
        if expected != actual:
            self.recon_mismatches += 1
            self._reconciliation_halt = True
            logger.error(
                "position mismatch: internal plan=%s vs venue positions=%d — "
                "FAIL CLOSED (reduce-only) until a clean pass",
                expected, account.open_positions,
            )
            self._rebuild_from_venue(expected=expected, venue_positions=account.open_positions)
            return
        # H2: existence agrees — but a WRONG-SIDE venue position (or a position on
        # ANOTHER symbol standing in for ours in a shared account) also diverges from
        # our model and must not read as "clean". When we believe we hold, verify the
        # venue's position FOR THIS SYMBOL matches our side.
        if expected and actual:
            divergence = await self._symbol_position_divergence()
            if divergence is not None:
                self.recon_mismatches += 1
                self._reconciliation_halt = True
                self._orphan_position = True
                self._journal_reconciliation_divergence(divergence)
                logger.error(
                    "position divergence on %s (%s) — FAIL CLOSED (reduce-only) "
                    "until an operator reconciles", self.symbol, divergence)
                return
        if self._reconciliation_halt:
            self._reconciliation_halt = False       # settled + agreeing → re-open
            self._orphan_position = False
            logger.info("position reconciliation clean — entries re-enabled")

    async def _symbol_position_divergence(self) -> str | None:
        """H2: compare the venue position FOR THIS SYMBOL against the internal plan's
        side. Returns a divergence reason or None. A read fault returns None — the
        account-state read already gates the read-failure fail-closed path; this only
        UPGRADES an existence-agreeing pass to a divergence, never masks a real one.
        Size is intentionally not gated (partial fills/funding drift legitimately),
        only the unambiguous wrong-side / wrong-symbol cases."""
        positions = await self._read_positions()
        if positions is None:
            return None
        pos = next((p for p in positions if getattr(p, "symbol", None) == self.symbol), None)
        if pos is None:
            # the account holds a position, but NOT on our symbol → we are actually
            # flat here while believing we hold (shared-account false agreement).
            return "no venue position on symbol while plan is held"
        plan_side = getattr(self._plan, "side", None)
        venue_side = getattr(pos, "side", None)
        if plan_side and venue_side and plan_side != venue_side:
            return f"side plan={plan_side} venue={venue_side}"
        return None

    def _journal_reconciliation_divergence(self, reason: str) -> None:
        journal = getattr(self.om, "_journal", None)
        if journal is None:
            return
        try:
            journal.append("reconciliation_divergence",
                           {"symbol": self.symbol, "reason": reason})
        except Exception as exc:  # noqa: BLE001 — journaling must not crash the loop
            logger.warning("divergence journal failed: %s", exc)

    def _rebuild_from_venue(self, *, expected: bool, venue_positions: int) -> None:
        """Rebuild INTERNAL state to venue truth on a settled mismatch — the
        invariant's 'rebuild state from the exchange'. The halt stays set; a clean
        pass re-opens entries.

        - we believe we hold but the venue is FLAT (external close / liquidation /
          a stop we missed): DROP the stale plan so the next settled pass agrees
          and entries auto-resume. Without this the halt would be PERMANENT — the
          plan never clears, so expected != actual forever.
        - the venue holds a position we DON'T track (orphan): we have no validated
          stop to manage it, so we stay reduce-only and surface it (flag + journal)
          for an operator flatten. The latch auto-clears once the orphan is gone.
        """
        self._journal_reconciliation(expected=expected, venue_positions=venue_positions)
        if expected and venue_positions == 0:
            logger.error("venue flat but internal plan present — dropping stale plan "
                         "(external close/liquidation); entries resume after a clean pass")
            self._clear_exit_plan()
            self._orphan_position = False
            return
        self._orphan_position = True   # untracked venue position → operator flatten
        logger.error("untracked venue position (%d) with no trade plan — reduce-only "
                     "until an operator flatten clears it", venue_positions)

    def _journal_reconciliation(self, *, expected: bool, venue_positions: int) -> None:
        journal = getattr(self.om, "_journal", None)
        if journal is None:
            return
        try:
            journal.append("reconciliation_rebuild", {
                "symbol": self.symbol,
                "internal_plan": expected,
                "venue_positions": venue_positions,
                "action": ("drop_stale_plan" if (expected and venue_positions == 0)
                           else "orphan_halt"),
            })
        except Exception as exc:  # noqa: BLE001 — journaling must not crash the loop
            logger.warning("reconciliation journal failed: %s", exc)

    def _resolve_pending_exit(self, client_order_id: str) -> None:
        base_key = next(
            (
                key
                for key, pending_id in self._pending_exit_orders.items()
                if pending_id == client_order_id
            ),
            None,
        )
        if base_key is None:
            return
        order = self.om.orders[client_order_id]
        if order.state in _EXIT_ACCEPTED_STATES:
            if self.protections is not None:
                parts = base_key.split("|")   # exit|SYMBOL|reason|ts
                if len(parts) >= 3:
                    self.protections.on_exit(parts[2], self._bars)
            self._clear_exit_plan()
            return
        if order.state in _EXIT_RETRYABLE_STATES:
            self._pending_exit_orders.pop(base_key, None)
            self._exit_retry_attempts[base_key] = self._exit_retry_attempts.get(base_key, 0) + 1

    def _ledger_sweep(self, now: datetime) -> None:
        """Append any OM order that reached an accepted/filled state and isn't
        chained yet. Resume-aware + deduped by client_order_id, so a restart
        continues the chain rather than re-recording. FAIL-SAFE: a ledger fault
        must never break the trading loop."""
        if self.fill_ledger is None:
            return
        try:
            bid, ask = getattr(self.feed, "quote", (None, None))
            ref = (float(bid) + float(ask)) / 2.0 if bid and ask else None
        except Exception:  # noqa: BLE001
            ref = None
        for coid, order in list(self.om.orders.items()):
            if coid in self._ledgered_orders or order.state not in _EXIT_ACCEPTED_STATES:
                continue
            try:
                self.fill_ledger.append({
                    "ts": now.isoformat(),
                    "mode": self.settings.trading_mode.value,
                    "venue": getattr(self.feed, "exchange_id", "live"),
                    "strategy_id": self.strategy.strategy_id,
                    "symbol": order.intent.symbol,
                    "side": "buy" if order.intent.side == "long" else "sell",
                    "quantity": order.intent.quantity,
                    "price": ref,                    # feed mid; exact fill px awaits the fill stream
                    "fee_usd": None,                 # unknown until the private fill stream
                    "realized_pnl_usd": None,
                    "client_order_id": coid,
                    "exchange_order_id": order.exchange_order_id,
                    "kind": "exit" if order.intent.reduce_only else "entry",
                    "state": order.state.value,
                    "record_type": "order_ack",      # honest: acceptance, not an enriched fill
                })
                self._ledgered_orders.add(coid)
            except Exception as exc:  # noqa: BLE001 — the ledger must not wedge the loop
                logger.error("fill ledger append failed for %s: %s", coid, exc)
                return

    def _report(self) -> RunReport:
        # L1: real equity / peak-drawdown / net-since-start from the venue account
        # truth. `fills` is the count of chained execution records (increment 2);
        # per-fill fees await the private fill stream, so fees_usd stays 0 here.
        # net_change is the account equity delta (realized + any open uPnL).
        net_change = (self._last_equity_usd - self._starting_equity_usd
                      if self._starting_equity_usd > 0 else 0.0)
        fills = self.fill_ledger.records if self.fill_ledger is not None else 0
        return RunReport(
            mode=self.settings.trading_mode.value, symbol=self.symbol,
            strategy_id=self.strategy.strategy_id, bars_processed=self._bars,
            signals_generated=self.signals, orders_submitted=self.orders_submitted,
            fills=fills, fees_usd=0.0, realized_pnl_usd=round(net_change, 6),
            unrealized_pnl_usd=0.0,
            max_drawdown_pct=round(self._max_drawdown_pct, 4),
            risk_rejects=self.risk_rejects,
            sizing_skips=self.sizing_skips, shadow_approved=0, shadow_rejected=0,
            reconciliation_mismatches=self.recon_mismatches,
            final_equity_usd=round(self._last_equity_usd, 6),
        )
