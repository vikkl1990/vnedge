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
