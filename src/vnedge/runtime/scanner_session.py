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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.execution.trigger_engine import TriggerConfig, TriggerEngine
from vnedge.plan.cost_model import CostModel
from vnedge.strategy.arm_sources import ArmSource, Bar, BarContext

UTC = dt.UTC


@dataclass(frozen=True, slots=True)
class SessionCosts:
    """Venue economics for a scanner session.

    Two modes, and the difference matters:

    * ``cost_model`` set -- costs come from ``vnedge.plan.cost_model``, the
      canonical source, and therefore include slippage and the safety buffer
      as well as fees. This is what a lane should use.
    * ``cost_model`` None -- the legacy FEE-ONLY behaviour. It understates the
      true cost by slip_in + slip_out + safety (8 bps on delta_scalp) and is
      retained only so older measurements remain reproducible.

    ``taker_bps`` defaults to 5.9 because that is Delta's all-in taker leg
    (5.0 x 1.18 GST) -- the same figure ``delta_scalp`` computes. It was
    hardcoded in three separate modules before this.
    """

    taker_bps: float = 5.9
    free_close_within_bars: int = 0  # legacy fee-only mode; never assumed on
    # Entry leg when it rests as a limit and is filled passively. None keeps
    # every entry taker, which is what all prior measurements assumed.
    maker_bps: float | None = None
    #: When set, every cost question is delegated here instead of to the
    #: fields above, so a lane cannot hold a private fee assumption.
    cost_model: CostModel | None = None
    bar_minutes: float = 5.0

    @classmethod
    def from_profile(cls, profile: str = "delta_scalp", *,
                     free_close_within_bars: int = 0,
                     bar_minutes: float = 5.0) -> SessionCosts:
        """Costs from the canonical model, slippage and safety included."""
        model = CostModel.for_profile(profile)
        return cls(
            taker_bps=model.fee_bps() * model.config.fee_gst_mult,
            maker_bps=model.fee_bps(maker=True) * model.config.fee_gst_mult,
            free_close_within_bars=free_close_within_bars,
            cost_model=model, bar_minutes=bar_minutes,
        )

    def round_trip_bps(self, held_bars: int, *, maker_entry: bool = False) -> float:
        if self.cost_model is not None:
            hold_minutes = held_bars * self.bar_minutes
            # The model itself owns any account-verified close waiver.  A free
            # close is not equivalent to a maker exit, and converting it here
            # silently applied a discount to the conservative delta profile.
            # The safety buffer is a PRE-TRADE gate margin, not a realized
            # venue charge, so completed trades exclude it from booked PnL.
            return self.cost_model.round_trip_bps(
                maker_entry=maker_entry,
                maker_exit=False,
                hold_minutes=hold_minutes,
                include_safety=False,
            )
        exit_leg = 0.0 if held_bars <= self.free_close_within_bars else self.taker_bps
        entry_leg = (
            self.maker_bps
            if maker_entry and self.maker_bps is not None
            else self.taker_bps
        )
        return entry_leg + exit_leg


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
    #: Fires when a resting limit is PLACED (not filled). The bar-level fill
    #: model is an assumption; this hook is what lets an L2 replay check it.
    on_pending: Callable[[dict], None] | None = None

    trades: list[ScannerTrade] = field(default_factory=list, repr=False)
    _open: dict | None = field(default=None, repr=False)
    _pending: dict | None = field(default=None, repr=False)
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
        if self._pending is not None:
            self._try_fill(bars, i, atr)
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
        if fire.pending:
            # A resting limit is not a position: it fills only if a LATER bar
            # trades to it, so the bar that produced the signal can never fill
            # it retroactively.
            self._pending = {
                "side": fire.side, "entry": fire.entry, "stop": fire.stop,
                "risk": fire.risk, "box_edge": fire.box_edge, "level": fire.level,
                "expires": fire.expires_bar, "chase_bps": fire.chase_bps,
                "reason": fire.reason,
                "arm": getattr(self.arm_source, "last_armed", None) or self.arm_source.name,
            }
            if self.on_pending is not None:
                self.on_pending({
                    "symbol": self.symbol, "placed_bar": i, "placed_ts_ms": bars[i][0],
                    "expires_ts_ms": bars[i][0] + (fire.expires_bar - i) * 300_000
                    if fire.expires_bar is not None else None,
                    **self._pending,
                })
            return
        self.exits.open_from_fire(
            side=fire.side, entry=fire.entry, stop=fire.stop, risk=fire.risk,
            box_edge=fire.box_edge, entry_bar=i,
        )
        self._open = {
            "side": fire.side, "entry": fire.entry, "bar": i, "ts": bars[i][0],
            "arm": getattr(self.arm_source, "last_armed", None) or self.arm_source.name,
            "chase_bps": fire.chase_bps, "reason": fire.reason, "stop": fire.stop,
            # the level the decision was anchored to -- NOT recoverable from
            # entry and stop, because a crossing entry sits beyond the level
            # while the stop is measured from it
            "level": fire.level,
        }
        if self.on_fire is not None:
            self.on_fire({"symbol": self.symbol, **self._open})

    def _try_fill(self, bars: Sequence[Bar], i: int, atr: float) -> None:
        """Fill a resting limit when this bar trades to it, else let it expire."""
        assert self._pending is not None
        p = self._pending
        touched = (
            bars[i][3] <= p["entry"] if p["side"] == "long" else bars[i][2] >= p["entry"]
        )
        if touched:
            self._pending = None
            self.exits.open_from_fire(
                side=p["side"], entry=p["entry"], stop=p["stop"], risk=p["risk"],
                box_edge=p["box_edge"], entry_bar=i,
            )
            self._open = {
                "side": p["side"], "entry": p["entry"], "bar": i, "ts": bars[i][0],
                "arm": p["arm"], "chase_bps": p["chase_bps"], "reason": p["reason"],
                "stop": p["stop"], "level": p["level"], "maker": True,
            }
            if self.on_fire is not None:
                self.on_fire({"symbol": self.symbol, **self._open})
            # The filling bar must still be managed. It traded THROUGH the
            # limit by construction, so it is the bar most likely to carry
            # price on to the stop; skipping it grants a free bar exactly
            # where the position is most exposed.
            self._manage(bars, i, atr)
            return
        if p["expires"] is not None and i >= p["expires"]:
            self._pending = None
            self.trigger.notify_cancelled(i)

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
        fee = self.costs.round_trip_bps(held, maker_entry=opened.get("maker", False))
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


def daily_returns_bps(trades: Sequence[ScannerTrade]) -> list[float]:
    """Per-UTC-day net bps, zero-filled across the span the trades cover.

    Zero-filling matters: a strategy that trades on 20 of 90 days has 70 days
    of zero return, and omitting them inflates every risk-adjusted statistic.
    """
    if not trades:
        return []
    buckets: dict[dt.date, float] = {}
    for trade in trades:
        day = trade.entry_time.date()
        buckets[day] = buckets.get(day, 0.0) + trade.net_bps
    first, last = min(buckets), max(buckets)
    span = (last - first).days + 1
    return [buckets.get(first + dt.timedelta(days=k), 0.0) for k in range(span)]


def summarize(trades: Sequence[ScannerTrade], notional_usd: float = 3000.0) -> dict:
    """Standard scorecard so every caller reports the same numbers.

    PSR is included because it is computable from one configuration.  DSR is
    NOT: deflating for multiple testing needs the dispersion of Sharpes across
    every config that was tried, which a single book cannot know.  Use
    :func:`family_metrics` for that -- asking one config for its own deflated
    Sharpe is the mistake that makes a searched result look pre-registered.
    """
    if not trades:
        return {"n": 0, "wins": 0, "pf": 0.0, "net_bps": 0.0, "net_usd": 0.0,
                "held_bars": 0, "max_dd_usd": 0.0, "psr": float("nan")}
    wins = [t for t in trades if t.net_bps > 0]
    gross_win = sum(t.net_bps for t in wins)
    gross_loss = -sum(t.net_bps for t in trades if t.net_bps <= 0)
    net = sum(t.net_bps for t in trades)

    equity = peak = 0.0
    max_dd = 0.0
    for trade in sorted(trades, key=lambda t: t.entry_ts_ms):
        equity += trade.net_bps * notional_usd / 1e4
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    daily = daily_returns_bps(trades)
    psr = float("nan")
    if len(daily) >= 8:
        from vnedge.ml.validation import probabilistic_sharpe_ratio

        psr = float(probabilistic_sharpe_ratio(daily))

    return {
        "n": len(trades),
        "wins": len(wins),
        "pf": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "net_bps": net,
        "net_usd": net * notional_usd / 1e4,
        "held_bars": sum(t.held_bars for t in trades),
        "max_dd_usd": max_dd,
        "psr": psr,
    }


def family_metrics(
    configs: Mapping[str, Sequence[ScannerTrade]],
    *,
    n_blocks: int = 10,
    n_trials: float | None = None,
) -> dict:
    """Multiple-testing statistics across a FAMILY of configurations.

    Both statistics here are family-level by construction:

    * PBO asks how often the in-sample winner lands below the out-of-sample
      median -- meaningless for a single config;
    * DSR deflates each config's Sharpe by the dispersion of Sharpes across
      the search, so it needs every variant that was tried.

    ``n_trials`` should be the HONEST total number of configurations explored
    (often far larger than ``len(configs)``, because discarded sweeps count).
    It defaults to the number of configs supplied, which is a floor, not a
    truth -- understating it flatters every DSR.
    """
    import numpy as np

    from vnedge.ml.validation import (
        deflated_sharpe_ratio,
        effective_number_of_trials,
        probability_of_backtest_overfitting,
    )

    series = {name: daily_returns_bps(t) for name, t in configs.items() if t}
    if len(series) < 2:
        return {"pbo": float("nan"), "dsr": {}, "effective_trials": float("nan")}
    width = min(len(v) for v in series.values())
    matrix = np.column_stack([v[-width:] for v in series.values()])

    sharpes = [
        float(np.mean(v) / np.std(v, ddof=1)) if np.std(v, ddof=1) > 0 else 0.0
        for v in (np.asarray(s[-width:], dtype=float) for s in series.values())
    ]
    trials = float(n_trials if n_trials is not None else len(series))
    dsr = {}
    if len(sharpes) >= 2:
        for name, values in series.items():
            dsr[name] = float(
                deflated_sharpe_ratio(
                    values[-width:], n_trials=trials, trial_sharpes=sharpes
                )
            )
    pbo = (
        float(probability_of_backtest_overfitting(matrix, n_blocks=n_blocks))
        if width >= n_blocks * 2
        else float("nan")
    )
    return {
        "pbo": pbo,
        "dsr": dsr,
        "effective_trials": float(effective_number_of_trials(matrix)),
        "nominal_trials": trials,
    }
