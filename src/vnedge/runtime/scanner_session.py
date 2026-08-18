"""One bar loop for every scanner path.

Before this module the same loop existed three times (two replay tools and
the shadow runner), each recomputing ATR, rolling VWAP, volume averages and
exit management.  Divergence between those copies is exactly the class of
bug the 2026-08 audit found across eight exit implementations, so the loop
now lives once and every caller drives it.

The session owns the mechanical parts -- per-bar features, the trigger and
exit engines, position bookkeeping, fee accounting -- and delegates the one
research variable, *where to look*, to a pluggable ``ArmSource``.

It journals nothing by itself: callers pass a sink and receive completed
``ScannerTrade`` records, so the same session serves a backtest, the
shadow lane, and a journal reconstruction without behavioural drift.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.execution.trigger_engine import TriggerConfig, TriggerEngine
from vnedge.strategy.arm_sources import ArmSource, Bar, BarContext

UTC = dt.timezone.utc


@dataclass(frozen=True, slots=True)
class SessionCosts:
    """Venue economics.  Delta all-in taker, with the Scalper Offer."""

    taker_bps: float = 5.9
    free_close_within_bars: int = 6  # 30 min on 5m bars; 0 disables the offer

    def round_trip_bps(self, held_bars: int) -> float:
        exit_leg = 0.0 if held_bars <= self.free_close_within_bars else self.taker_bps
        return self.taker_bps + exit_leg


@dataclass(frozen=True, slots=True)
class SessionConfig:
    atr_period: int = 48
    volume_lookback: int = 48
    vwap_bars: int = 288


@dataclass(frozen=True, slots=True)
class ScannerTrade:
    symbol: str
    arm: str
    side: str
    entry_index: int
    exit_index: int
    entry_ts_ms: int
    exit_ts_ms: int
    entry_price: float
    exit_price: float
    reason: str
    held_bars: int
    net_bps: float
    gross_bps: float
    fee_bps: float
    chase_bps: float

    @property
    def entry_time(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.entry_ts_ms / 1000, UTC)

    @property
    def exit_time(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.exit_ts_ms / 1000, UTC)


@dataclass
class ScannerSession:
    """Drive one symbol's bars through arm -> fire -> manage."""

    symbol: str
    arm_source: ArmSource
    trigger: TriggerEngine = field(default_factory=lambda: TriggerEngine(config=TriggerConfig()))
    exits: ExitEngine = field(default_factory=lambda: ExitEngine(config=ExitConfig()))
    costs: SessionCosts = field(default_factory=SessionCosts)
    config: SessionConfig = field(default_factory=SessionConfig)
    on_fire: Callable[[dict], None] | None = None
    on_close: Callable[[ScannerTrade], None] | None = None

    trades: list[ScannerTrade] = field(default_factory=list, repr=False)
    _open: dict | None = field(default=None, repr=False)
    _pv: float = field(default=0.0, repr=False)
    _vv: float = field(default=0.0, repr=False)

    # --- per-bar features -----------------------------------------------
    def _atr(self, bars: Sequence[Bar], i: int) -> float:
        period = self.config.atr_period
        if i < period + 1:
            return 0.0
        return statistics.mean(
            max(
                bars[j][2] - bars[j][3],
                abs(bars[j][2] - bars[j - 1][4]),
                abs(bars[j][3] - bars[j - 1][4]),
            )
            for j in range(i - period, i)
        )

    def _roll_vwap(self, bars: Sequence[Bar], i: int) -> float | None:
        if i >= 1:
            j = i - 1
            self._pv += bars[j][4] * bars[j][5]
            self._vv += bars[j][5]
            if i - 1 >= self.config.vwap_bars:
                k = i - 1 - self.config.vwap_bars
                self._pv -= bars[k][4] * bars[k][5]
                self._vv -= bars[k][5]
        return self._pv / self._vv if self._vv > 0 else None

    # --- main loop --------------------------------------------------------
    def run(self, bars: Sequence[Bar], *, start_ms: int | None = None) -> list[ScannerTrade]:
        for i in range(len(bars)):
            self.step(bars, i, start_ms=start_ms)
        return self.trades

    def step(self, bars: Sequence[Bar], i: int, *, start_ms: int | None = None) -> None:
        vwap = self._roll_vwap(bars, i)
        lookback = self.config.volume_lookback
        if i < max(self.config.atr_period, lookback) + 1:
            return
        atr = self._atr(bars, i)
        vol_ma = statistics.mean(b[5] for b in bars[i - lookback : i])
        ctx = BarContext(
            bars=bars, index=i, atr=atr, vol_ma=vol_ma, vwap=vwap,
            prev_close=bars[i - 1][4],
        )

        # The arm source observes EVERY bar, including while a position is open
        # and before the reporting window, so its rolling state never develops
        # gaps.  Whether the arm is acted on is a separate decision below.
        arm = self.arm_source.observe(ctx)

        if self._open is not None:
            self._manage(bars, i, atr)
            return
        if start_ms is not None and bars[i][0] < start_ms:
            return
        if arm is None:
            return
        fire = self.trigger.try_fire(
            arm=arm, high=bars[i][2], low=bars[i][3], close=bars[i][4],
            volume=bars[i][5], vwap=vwap, bar_index=i, bar_ts_ms=bars[i][0],
        )
        if fire is None:
            return
        self.exits.open_from_fire(
            side=fire.side, entry=fire.entry, stop=fire.stop, risk=fire.risk,
            box_edge=fire.box_edge, entry_bar=i,
        )
        self._open = {
            "side": fire.side, "entry": fire.entry, "bar": i, "ts": bars[i][0],
            "arm": getattr(self.arm_source, "last_armed", None) or self.arm_source.name,
            "chase_bps": fire.chase_bps, "reason": fire.reason, "stop": fire.stop,
        }
        if self.on_fire is not None:
            self.on_fire({"symbol": self.symbol, **self._open})

    def _manage(self, bars: Sequence[Bar], i: int, atr: float) -> None:
        assert self._open is not None
        decision = self.exits.on_bar(
            high=bars[i][2], low=bars[i][3], close=bars[i][4], atr=atr, bar_index=i
        )
        if decision is None:
            return
        opened = self._open
        held = i - opened["bar"]
        side = opened["side"]
        gross = (
            (decision.price / opened["entry"] - 1)
            if side == "long"
            else (1 - decision.price / opened["entry"])
        ) * 1e4
        fee = self.costs.round_trip_bps(held)
        trade = ScannerTrade(
            symbol=self.symbol, arm=opened["arm"], side=side,
            entry_index=opened["bar"], exit_index=i,
            entry_ts_ms=opened["ts"], exit_ts_ms=bars[i][0],
            entry_price=opened["entry"], exit_price=decision.price,
            reason=decision.reason, held_bars=held,
            net_bps=gross - fee, gross_bps=gross, fee_bps=fee,
            chase_bps=opened["chase_bps"],
        )
        self.trades.append(trade)
        self.trigger.notify_flat(i, won=decision.won)
        self._open = None
        if self.on_close is not None:
            self.on_close(trade)


def summarize(trades: Sequence[ScannerTrade], notional_usd: float = 3000.0) -> dict:
    """Standard scorecard so every caller reports the same numbers."""
    if not trades:
        return {"n": 0, "wins": 0, "pf": 0.0, "net_bps": 0.0, "net_usd": 0.0,
                "held_bars": 0}
    wins = [t for t in trades if t.net_bps > 0]
    gross_win = sum(t.net_bps for t in wins)
    gross_loss = -sum(t.net_bps for t in trades if t.net_bps <= 0)
    net = sum(t.net_bps for t in trades)
    return {
        "n": len(trades),
        "wins": len(wins),
        "pf": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "net_bps": net,
        "net_usd": net * notional_usd / 1e4,
        "held_bars": sum(t.held_bars for t in trades),
    }
