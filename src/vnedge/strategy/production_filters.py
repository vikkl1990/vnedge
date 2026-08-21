"""Production gating plane: regime, session, EV and veto layers over an arm.

The Structure Bounce sequence is a *candidate generator*.  The claim under
test is that the win rate comes from the stack bolted on top of it -- regime
detection, session gating, expectancy screening, a confluence/HTF requirement
and a fee-aware edge check -- not from the sequence itself.

Every layer here is causal by construction:

* the regime detector is a streaming Wilder ADX plus rolling Bollinger
  bandwidth and ATR%, updated bar by bar and never re-read;
* the bandwidth percentile ranks against a trailing window only;
* the expectancy engine is fed *closed* trades as they resolve, so a trade is
  screened using outcomes that were already knowable when it was taken.

That last point is the whole ballgame.  An EV filter fitted on the same
trades it screens is a look-ahead machine: it rejects the losers because it
has already seen them lose.  ``ExpectancyEngine`` therefore refuses to score
a bucket until ``min_samples`` trades have *closed*, and its statistics are
append-only.

The ML re-ranker described alongside these layers is deliberately NOT
implemented: a scorer trained on the window it is evaluated on inflates
every number it touches, and doing it honestly needs its own purged
walk-forward.  Its absence is a stated gap, not an oversight.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from vnedge.execution.trigger_engine import ArmState
from vnedge.strategy.arm_sources import BarContext

Regime = Literal["trending", "expansion", "low_liquidity", "high_vol_chop", "range"]


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    label: Regime
    adx: float
    atr_pct: float
    bb_bandwidth: float
    bb_rank: float
    volume_ratio: float
    direction: Literal["up", "down", "flat"]


@dataclass
class StreamingRegime:
    """Wilder ADX + Bollinger bandwidth, updated once per closed bar."""

    adx_period: int = 14
    bb_period: int = 20
    bb_rank_window: int = 288
    trend_adx: float = 30.0
    chop_adx: float = 20.0
    expansion_rank: float = 0.92
    quiet_volume: float = 0.6

    _prev: tuple | None = field(default=None, repr=False)
    _atr: float = field(default=0.0, repr=False)
    _plus: float = field(default=0.0, repr=False)
    _minus: float = field(default=0.0, repr=False)
    _dx: deque = field(default_factory=lambda: deque(maxlen=14), repr=False)
    _adx: float = field(default=0.0, repr=False)
    _closes: deque = field(default_factory=lambda: deque(maxlen=20), repr=False)
    _ranks: deque = field(default_factory=lambda: deque(maxlen=288), repr=False)
    #: Weekday-only bandwidth history. Weekends run 0.54-0.58x the weekday
    #: median hourly range on 0.43-0.46x the volume (measured 2026-08-21), so a
    #: percentile pooled across both is loosest exactly where liquidity is
    #: worst. A caller that trades weekdays only must rank against weekdays.
    _weekday_ranks: deque = field(default_factory=lambda: deque(maxlen=288), repr=False)
    _n: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self._dx = deque(maxlen=self.adx_period)
        self._closes = deque(maxlen=self.bb_period)
        self._ranks = deque(maxlen=self.bb_rank_window)
        self._weekday_ranks = deque(maxlen=self.bb_rank_window)

    def update(self, bar, *, vol_ma: float, weekday: bool = True) -> RegimeSnapshot:
        _, _, high, low, close, volume = bar
        n = self.adx_period
        if self._prev is not None:
            p_high, p_low, p_close = self._prev
            up_move, down_move = high - p_high, p_low - low
            plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
            minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0
            tr = max(high - low, abs(high - p_close), abs(low - p_close))
            # Wilder smoothing: seed with the first value, then decay 1/n
            if self._n < n:
                self._atr += tr
                self._plus += plus_dm
                self._minus += minus_dm
            else:
                self._atr = self._atr - self._atr / n + tr
                self._plus = self._plus - self._plus / n + plus_dm
                self._minus = self._minus - self._minus / n + minus_dm
            self._n += 1
            if self._atr > 0 and self._n >= n:
                pdi = 100.0 * self._plus / self._atr
                mdi = 100.0 * self._minus / self._atr
                total = pdi + mdi
                dx = 100.0 * abs(pdi - mdi) / total if total > 0 else 0.0
                self._dx.append(dx)
                self._adx = sum(self._dx) / len(self._dx)
        self._prev = (high, low, close)

        self._closes.append(close)
        bandwidth = 0.0
        if len(self._closes) == self.bb_period:
            mean = sum(self._closes) / self.bb_period
            var = sum((c - mean) ** 2 for c in self._closes) / self.bb_period
            bandwidth = (4.0 * var**0.5) / mean if mean > 0 else 0.0
            self._ranks.append(bandwidth)
            if weekday:
                self._weekday_ranks.append(bandwidth)
        pool = self._weekday_ranks if (weekday and self._weekday_ranks) else self._ranks
        rank = (
            sum(1 for b in pool if b <= bandwidth) / len(pool) if pool else 0.0
        )
        atr_pct = (self._atr / n) / close if close > 0 and self._n >= n else 0.0
        vol_ratio = volume / vol_ma if vol_ma > 0 else 0.0

        pdi = 100.0 * self._plus / self._atr if self._atr > 0 else 0.0
        mdi = 100.0 * self._minus / self._atr if self._atr > 0 else 0.0
        direction = "up" if pdi > mdi else "down" if mdi > pdi else "flat"

        if vol_ratio < self.quiet_volume:
            label: Regime = "low_liquidity"
        elif rank >= self.expansion_rank:
            label = "expansion"
        elif self._adx >= self.trend_adx:
            label = "trending"
        elif self._adx < self.chop_adx and rank >= 0.75:
            label = "high_vol_chop"
        else:
            label = "range"
        return RegimeSnapshot(
            label=label, adx=self._adx, atr_pct=atr_pct, bb_bandwidth=bandwidth,
            bb_rank=rank, volume_ratio=vol_ratio, direction=direction,
        )


@dataclass
class StochObvFilter:
    """Stochastic %K extremes and OBV agreement, both streaming.

    Blocks buying into overbought and selling into oversold, and blocks a
    trade whose on-balance volume is moving against it.  Both read only closed
    bars.
    """

    stoch_period: int = 14
    overbought: float = 80.0
    oversold: float = 20.0
    obv_slope_bars: int = 20

    _highs: deque = field(default_factory=lambda: deque(maxlen=14), repr=False)
    _lows: deque = field(default_factory=lambda: deque(maxlen=14), repr=False)
    _obv: float = field(default=0.0, repr=False)
    _obv_hist: deque = field(default_factory=lambda: deque(maxlen=21), repr=False)
    _prev_close: float | None = field(default=None, repr=False)
    last_stoch: float = field(default=50.0, repr=False)
    last_obv_slope: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self._highs = deque(maxlen=self.stoch_period)
        self._lows = deque(maxlen=self.stoch_period)
        self._obv_hist = deque(maxlen=self.obv_slope_bars + 1)

    def update(self, bar) -> None:
        _, _, high, low, close, volume = bar
        self._highs.append(high)
        self._lows.append(low)
        top, bottom = max(self._highs), min(self._lows)
        self.last_stoch = (
            100.0 * (close - bottom) / (top - bottom) if top > bottom else 50.0
        )
        if self._prev_close is not None:
            if close > self._prev_close:
                self._obv += volume
            elif close < self._prev_close:
                self._obv -= volume
        self._prev_close = close
        self._obv_hist.append(self._obv)
        if len(self._obv_hist) >= 2:
            self.last_obv_slope = self._obv_hist[-1] - self._obv_hist[0]

    def blocks(self, side: str) -> str | None:
        """Return the blocking reason, or None when the trade may proceed."""
        if len(self._highs) < self.stoch_period:
            return None
        if side == "long" and self.last_stoch > self.overbought:
            return "stoch_overbought"
        if side == "short" and self.last_stoch < self.oversold:
            return "stoch_oversold"
        if len(self._obv_hist) < self.obv_slope_bars:
            return None
        if side == "long" and self.last_obv_slope < 0:
            return "obv_against"
        if side == "short" and self.last_obv_slope > 0:
            return "obv_against"
        return None


# Session confidence deltas, UTC. Mirrors the documented learned adjustments.
SESSION_ADJUSTMENT: dict[str, int] = {
    "asia_early": 0, "asia_late": -15, "europe": 5,
    "us": 5, "us_late": 0, "off_hours": -5,
}


def session_of(ts_ms: int) -> str:
    hour = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).hour
    if hour < 3:
        return "asia_early"
    if hour < 7:
        return "asia_late"
    if hour < 12:
        return "europe"
    if hour < 17:
        return "us"
    if hour < 21:
        return "us_late"
    return "off_hours"


@dataclass
class ExpectancyEngine:
    """Per-bucket expectancy from CLOSED trades only.

    ``observe`` is called when a trade resolves; ``verdict`` is called before
    one is taken.  The engine never sees an outcome it could not already have
    known, so a rejection here is a real filter rather than hindsight.
    """

    min_samples: int = 20
    reject_below: float = 0.0
    reduce_below: float = 0.10
    _wins: dict[str, int] = field(default_factory=dict, repr=False)
    _losses: dict[str, int] = field(default_factory=dict, repr=False)
    _win_r: dict[str, float] = field(default_factory=dict, repr=False)
    _loss_r: dict[str, float] = field(default_factory=dict, repr=False)

    def observe(self, bucket: str, r_multiple: float) -> None:
        if r_multiple > 0:
            self._wins[bucket] = self._wins.get(bucket, 0) + 1
            self._win_r[bucket] = self._win_r.get(bucket, 0.0) + r_multiple
        else:
            self._losses[bucket] = self._losses.get(bucket, 0) + 1
            self._loss_r[bucket] = self._loss_r.get(bucket, 0.0) + abs(r_multiple)

    def expectancy(self, bucket: str) -> float | None:
        wins, losses = self._wins.get(bucket, 0), self._losses.get(bucket, 0)
        total = wins + losses
        if total < self.min_samples:
            return None
        p_win = wins / total
        avg_win = self._win_r.get(bucket, 0.0) / wins if wins else 0.0
        avg_loss = self._loss_r.get(bucket, 0.0) / losses if losses else 0.0
        return p_win * avg_win - (1.0 - p_win) * avg_loss

    def verdict(self, bucket: str) -> Literal["TRADE", "REDUCED", "REJECT"]:
        ev = self.expectancy(bucket)
        if ev is None:
            return "TRADE"  # insufficient history is not evidence of no edge
        if ev < self.reject_below:
            return "REJECT"
        if ev < self.reduce_below:
            return "REDUCED"
        return "TRADE"


@dataclass
class ProductionGate:
    """Wraps an arm source and applies the production gating stack.

    Each layer can be switched off independently so its contribution is
    measurable rather than asserted.
    """

    inner: object
    name: str = "structure_bounce_prod"

    use_regime: bool = True
    use_session: bool = True
    use_ev: bool = True
    use_counter_trend_block: bool = True
    use_confluence_required: bool = True
    use_fee_check: bool = True
    use_stoch_obv: bool = False
    #: Hard UTC hour gate. None trades every hour. A confidence adjustment
    #: (``use_session``) only nudges the score -- it still lets a marginal
    #: setup through in a dead hour, which is a different thing from not
    #: trading then. Measured 2026-08-21 over 30 days: BTC's median hourly
    #: range is 66 bps at 14:00 UTC against 24 bps at 20:00, so a ~17.8 bps
    #: round trip consumes 27% of the hour's range at the peak and 68% at
    #: the trough. The gate exists to stop paying the second price.
    #: Stand down Sat/Sun entirely. Separate from allowed_hours because the
    #: weekend is a different liquidity regime, not merely a different hour:
    #: range differs by weekday at p=1.5e-13 (BTC) / 1.4e-10 (ETH).
    weekday_only: bool = False
    allowed_hours: tuple[int, ...] | None = None
    #: KNOWN DEFECT, measured 2026-08-21: this percentile is computed over a
    #: POOLED rolling window that mixes weekdays and weekends. Range differs by
    #: weekday at p=1.5e-13 (BTC) / 1.4e-10 (ETH), and weekends run 0.54-0.58x
    #: the weekday median hourly range on 0.43-0.46x the volume. A pooled p50
    #: therefore admits weekend hours a weekday-only p50 would reject -- the
    #: floor is loosest exactly where liquidity is worst and fills are hardest.
    #: Any future use should stratify by weekday/weekend, or gate weekends out.
    #: Left as-is rather than silently changed: the strategy that used this
    #: failed its sealed run (docs/prereg/bounce_vol_band_20260821.md) and
    #: refitting a closed experiment's gate is exactly what that verdict forbids.
    #:
    #: Require volatility to be AT LEAST this Bollinger-bandwidth percentile.
    #: Note the tension with ``use_regime``, which blocks the top 8% as
    #: "expansion": one gate refuses the widest conditions and this one demands
    #: them. Volatility clusters strongly on our own data (hourly |return|
    #: autocorrelation 0.24-0.27, realized range 0.42-0.55, GARCH persistence
    #: 0.985 with a ~45h half-life), so conditional width is forecastable --
    #: but a wide distribution only makes an after-cost edge POSSIBLE, it does
    #: not create one, and adverse selection rises with it too.
    min_bb_rank: float | None = None
    min_confidence: int = 65
    fee_cover_mult: float = 2.5
    round_trip_bps: float = 11.8
    tp1_r: float = 1.5
    stop_pct_floor: float = 0.0055
    min_tp1_pct: float = 0.0065

    regime: StreamingRegime = field(default_factory=StreamingRegime)
    stoch_obv: StochObvFilter = field(default_factory=StochObvFilter)
    ev: ExpectancyEngine = field(default_factory=ExpectancyEngine)
    last_regime: RegimeSnapshot | None = field(default=None, repr=False)
    last_bucket: str = field(default="", repr=False)
    blocked: dict[str, int] = field(default_factory=dict, repr=False)
    passed: int = field(default=0, repr=False)

    @property
    def warmup_bars(self) -> int:
        return getattr(self.inner, "warmup_bars", 300)

    @property
    def last_armed(self) -> str:
        return self.name

    def _block(self, reason: str) -> None:
        self.blocked[reason] = self.blocked.get(reason, 0) + 1

    def observe(self, ctx: BarContext) -> ArmState | None:
        stamp = datetime.fromtimestamp(ctx.bars[ctx.index][0] / 1000, tz=UTC)
        is_weekday = stamp.weekday() < 5
        snapshot = self.regime.update(
            ctx.bars[ctx.index], vol_ma=ctx.vol_ma, weekday=is_weekday
        )
        self.stoch_obv.update(ctx.bars[ctx.index])
        self.last_regime = snapshot
        arm = self.inner.observe(ctx)
        if arm is None:
            return None
        side = arm.side_hint
        if side is None:
            return arm

        confidence = getattr(self.inner, "last_confidence", 0)
        reason = getattr(self.inner, "last_reason", "")

        if self.weekday_only and not is_weekday:
            self._block("weekend")
            return None

        if self.min_bb_rank is not None and snapshot.bb_rank < self.min_bb_rank:
            self._block("too_quiet")
            return None

        if self.allowed_hours is not None:
            hour = stamp.hour
            if hour not in self.allowed_hours:
                self._block("session_hour")
                return None

        if self.use_stoch_obv:
            blocked = self.stoch_obv.blocks(side)
            if blocked is not None:
                self._block(blocked)
                return None

        if self.use_regime and snapshot.label in ("low_liquidity", "expansion"):
            self._block(f"regime:{snapshot.label}")
            return None

        if self.use_counter_trend_block and snapshot.label == "trending":
            against = (
                (snapshot.direction == "up" and side == "short")
                or (snapshot.direction == "down" and side == "long")
            )
            if against:
                self._block("counter_trend")
                return None

        if self.use_confluence_required and "confluence=0" in reason:
            self._block("no_confluence")
            return None

        if self.use_session:
            confidence += SESSION_ADJUSTMENT.get(session_of(ctx.bars[ctx.index][0]), 0)
        if confidence < self.min_confidence:
            self._block("min_confidence")
            return None

        if self.use_fee_check:
            close = ctx.bars[ctx.index][4]
            risk_pct = max(self.stop_pct_floor, 0.0)
            tp1_pct = self.tp1_r * risk_pct
            need = max(
                self.min_tp1_pct,
                self.fee_cover_mult * self.round_trip_bps / 10_000.0,
            )
            if close <= 0 or tp1_pct < need:
                self._block("fee_cover")
                return None

        self.last_bucket = f"{snapshot.label}|{side}"
        if self.use_ev and self.ev.verdict(self.last_bucket) == "REJECT":
            self._block("negative_ev")
            return None

        self.passed += 1
        return arm

    def on_trade_closed(self, *, bucket: str, r_multiple: float) -> None:
        self.ev.observe(bucket, r_multiple)
