"""Pluggable arm sources: the part of a scanner that decides WHERE to look.

The 2026-08-19 ablation established the division of labour this module
encodes: the trigger and exit engines carry the edge, and the arming
condition is the variable being researched.  Making arms pluggable lets a
new hypothesis be tested by writing one small class instead of another
copy of the bar loop.

An arm source converts a closed-bar context into an ``ArmState`` (the
frozen snapshot ``TriggerEngine`` consumes) or ``None``.  Sources are
causal by construction: they receive only bars at or before the index and
compute their levels from bars strictly before it.

Sources own their episode numbering so two arms can never burn each
other's latch.  Coil episodes count up from 1; ignition episodes are
negative and unique per bar, because an ignition is a single-bar event
rather than a state the market sits in.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from vnedge.execution.trigger_engine import ArmState

# bars are (open_time_ms, open, high, low, close, volume)
Bar = tuple[int, float, float, float, float, float]


@dataclass(frozen=True, slots=True)
class BarContext:
    """Shared per-bar features, computed once by the session."""

    bars: Sequence[Bar]
    index: int
    atr: float
    vol_ma: float
    vwap: float | None
    prev_close: float

    @property
    def bar(self) -> Bar:
        return self.bars[self.index]

    def box(self, lookback: int) -> tuple[float, float]:
        """High/low of the ``lookback`` bars BEFORE this one."""
        window = self.bars[self.index - lookback : self.index]
        return max(b[2] for b in window), min(b[3] for b in window)


class ArmSource(Protocol):
    """Decides whether this bar presents an armable setup."""

    name: str

    def observe(self, ctx: BarContext) -> ArmState | None: ...


@dataclass
class CoilArmSource:
    """Compression arm: the box ranks in the narrowest tail of its history.

    Stateful across bars only for the rank window and the episode counter;
    both depend solely on bars already seen.
    """

    name: str = "coil"
    compression_bars: int = 48
    rank_lookback: int = 2016
    threshold: float = 0.20
    absolute_floor_bps: float | None = None
    _ranks: list[float] = field(default_factory=list, repr=False)
    _episode: int = field(default=0, repr=False)
    _prev_compressed: bool = field(default=False, repr=False)

    @property
    def warmup_bars(self) -> int:
        return self.rank_lookback + self.compression_bars + 1

    def observe(self, ctx: BarContext) -> ArmState | None:
        if ctx.index < self.compression_bars + 1:
            return None
        box_high, box_low = ctx.box(self.compression_bars)
        span = (box_high - box_low) / ctx.prev_close if ctx.prev_close else 0.0
        self._ranks.append(span)
        if len(self._ranks) > self.rank_lookback:
            self._ranks.pop(0)
        if len(self._ranks) < self.rank_lookback:
            self._prev_compressed = False
            return None
        rank = sum(1 for x in self._ranks if x < span) / len(self._ranks)
        compressed = rank <= self.threshold
        if not compressed and self.absolute_floor_bps is not None:
            compressed = span * 1e4 <= self.absolute_floor_bps
        if compressed and not self._prev_compressed:
            self._episode += 1
        self._prev_compressed = compressed
        if not compressed:
            return None
        return ArmState(
            episode_id=self._episode,
            box_high=box_high,
            box_low=box_low,
            compressed=True,
            atr=ctx.atr,
            vol_ma=ctx.vol_ma,
            prev_close=ctx.prev_close,
        )


@dataclass
class IgnitionArmSource:
    """Thrust arm: a wide-bodied bar on heavy volume clearing a recent box.

    This is the fee-wall observer's trigger.  It failed live (PF 0.61) with
    chase entries and a fixed target; here it is only an *arm*, and the
    trigger/exit engines supply the discipline it lacked.
    """

    name: str = "ignition"
    box_bars: int = 24
    body_fraction: float = 0.60
    volume_mult: float = 2.5

    @property
    def warmup_bars(self) -> int:
        return self.box_bars + 1

    def observe(self, ctx: BarContext) -> ArmState | None:
        if ctx.index < self.box_bars + 1 or ctx.vol_ma <= 0:
            return None
        bar = ctx.bar
        span = bar[2] - bar[3]
        if span <= 0:
            return None
        body = abs(bar[4] - bar[1])
        if body < self.body_fraction * span:
            return None
        if bar[5] < self.volume_mult * ctx.vol_ma:
            return None
        box_high, box_low = ctx.box(self.box_bars)
        return ArmState(
            # negative, bar-unique: an ignition is an event, not a state, and
            # must never share a latch with the coil source
            episode_id=-(ctx.index + 1),
            box_high=box_high,
            box_low=box_low,
            compressed=True,
            atr=ctx.atr,
            vol_ma=ctx.vol_ma,
            prev_close=ctx.prev_close,
        )


@dataclass
class CompositeArmSource:
    """First source to arm wins; order encodes priority."""

    sources: Sequence[ArmSource]
    name: str = "composite"
    last_armed: str | None = field(default=None, repr=False)

    @property
    def warmup_bars(self) -> int:
        return max(getattr(s, "warmup_bars", 0) for s in self.sources)

    def observe(self, ctx: BarContext) -> ArmState | None:
        armed: ArmState | None = None
        winner: str | None = None
        for source in self.sources:
            # every source observes every bar so stateful ones stay warm
            state = source.observe(ctx)
            if armed is None and state is not None:
                armed, winner = state, source.name
        self.last_armed = winner
        return armed


@dataclass
class StructureBounceArmSource:
    """Sequence-based bounce off the nearest structure zone.

    A port of the Structure Bounce scanner.  Unlike the breakout arms, this one
    decides its own direction (support rejection -> long, resistance rejection
    -> short) and sets ``side_hint`` so the trigger anchors on the zone edge
    being defended rather than a level being broken.

    All four events must occur, in order, for an arm to be produced:

    1. APPROACH   a bar within the last ``sequence_bars`` traded into the zone
                  (or within ``approach_pct`` of the level);
    2. REJECTION  that bar left a wick beyond ``wick_frac`` of its range and
                  closed back on the favourable side;
    3. VOLUME     the rejection bar or the current bar carried volume;
    4. CONFIRM    the current bar closes in the signal direction, and -- when
                  the rejection was earlier -- back across the level.

    Confidence is scored additively (wick quality, volume, level strength,
    touch count, confluence) and gated by ``min_confidence``.  The score is a
    *filter*, not a size input: nothing downstream reads it.

    The structure map is rebuilt every ``rebuild_every`` bars rather than every
    bar.  Structure does not change bar to bar, and the liquidity detector is
    O(swings^2); rebuilding each bar makes a 90-day replay impractical.
    """

    name: str = "structure_bounce"
    sequence_bars: int = 5
    approach_pct: float = 0.5
    wick_frac: float = 0.30
    volume_strong: float = 1.5
    volume_ok: float = 1.2
    min_confidence: int = 50
    rebuild_every: int = 12
    map_bars: int = 300
    _map: object | None = field(default=None, repr=False)
    _map_index: int = field(default=-10**9, repr=False)
    _episode: int = field(default=0, repr=False)
    last_confidence: int = field(default=0, repr=False)
    last_reason: str = field(default="", repr=False)

    @property
    def warmup_bars(self) -> int:
        return self.map_bars + 2

    def _refresh_map(self, ctx: BarContext) -> None:
        from vnedge.strategy.structure_map import build_structure_map

        window = ctx.bars[max(0, ctx.index - self.map_bars) : ctx.index + 1]
        if len(window) < 20:
            self._map = None
            return
        atrs = [ctx.atr] * len(window)  # one ATR source, consistently applied
        self._map = build_structure_map(window, atrs, atr=ctx.atr)
        self._map_index = ctx.index

    def observe(self, ctx: BarContext) -> ArmState | None:
        if ctx.index < self.warmup_bars or ctx.atr <= 0 or ctx.vol_ma <= 0:
            return None
        if ctx.index - self._map_index >= self.rebuild_every:
            self._refresh_map(ctx)
        structure = self._map
        if structure is None:
            return None

        bars = ctx.bars
        current = bars[ctx.index]
        c_open, c_high, c_low, c_close = current[1], current[2], current[3], current[4]
        if c_high <= c_low:
            return None

        side: str | None = None
        level = None
        rejection_offset = 0
        score = 0

        for offset in range(0, min(self.sequence_bars, ctx.index)):
            bar = bars[ctx.index - offset]
            b_open, b_high, b_low, b_close = bar[1], bar[2], bar[3], bar[4]
            span = b_high - b_low
            if span <= 0 or b_close <= 0:
                continue

            support = structure.nearest_support
            if side is None and support is not None:
                in_zone = support.contains(b_low)
                near = abs((b_low - support.price) / b_close * 100) < self.approach_pct
                if in_zone or near:
                    wick = min(b_open, b_close) - b_low
                    if wick > span * self.wick_frac and b_close > b_low + span * 0.45:
                        side, level, rejection_offset = "long", support, offset
                        score += 30
                        if wick > ctx.atr * 0.5:
                            score += 15
                        elif wick > ctx.atr * 0.3:
                            score += 10
                        if in_zone:
                            score += 5

            resistance = structure.nearest_resistance
            if side is None and resistance is not None:
                in_zone = resistance.contains(b_high)
                near = abs((resistance.price - b_high) / b_close * 100) < self.approach_pct
                if in_zone or near:
                    wick = b_high - max(b_open, b_close)
                    if wick > span * self.wick_frac and b_close < b_low + span * 0.55:
                        side, level, rejection_offset = "short", resistance, offset
                        score += 30
                        if wick > ctx.atr * 0.5:
                            score += 15
                        elif wick > ctx.atr * 0.3:
                            score += 10
                        if in_zone:
                            score += 5
            if side is not None:
                break

        if side is None or level is None:
            return None

        rejection = bars[ctx.index - rejection_offset]
        best_volume = max(rejection[5], current[5]) / ctx.vol_ma
        if best_volume > self.volume_strong:
            score += 15
        elif best_volume > self.volume_ok:
            score += 10
        elif best_volume > 0.9:
            score += 5
        else:
            score -= 5

        if rejection_offset == 0:
            if side == "long" and c_close <= c_open:
                return None
            if side == "short" and c_close >= c_open:
                return None
        else:
            if side == "long" and (c_close <= c_open or c_close < level.price):
                return None
            if side == "short" and (c_close >= c_open or c_close > level.price):
                return None
            score += 5

        score += min(level.strength // 5, 15)
        if level.touch_count >= 3:
            score += 10
        confluence = [
            other for other in structure.levels
            if other is not level and abs(other.price - level.price) / c_close < 0.003
        ]
        if confluence:
            score += 10

        confidence = max(0, min(score, 100))
        self.last_confidence = confidence
        if confidence < self.min_confidence:
            return None

        self._episode += 1
        self.last_reason = (
            f"structure_bounce {side} {level.level_type} conf={confidence} "
            f"strength={level.strength} touches={level.touch_count} "
            f"vol={best_volume:.1f}x confluence={len(confluence)}"
        )
        return ArmState(
            episode_id=self._episode,
            box_high=level.zone_high,
            box_low=level.zone_low,
            compressed=True,
            atr=ctx.atr,
            vol_ma=ctx.vol_ma,
            prev_close=ctx.prev_close,
            side_hint=side,
        )
