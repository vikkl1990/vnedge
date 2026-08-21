"""Paired maker / taker lanes for the structure-bounce production stack.

The maker result rests on a fee assumption bar data cannot settle: it books
passive entries at ``maker_bps`` and assumes a resting limit fills whenever
price touches it.  Neither holds automatically -- real passive fills need
queue position, and the fills you reliably get at a defended level are the
adversely-selected ones.

Rather than pick a side, both lanes run.  They are identical in every plane
except entry and fee, so a divergence between them is attributable to that
difference and nothing else:

* arm source, structure map, gating stack, stops and exits are shared;
* the TAKER lane crosses the spread at confirmation close;
* the MAKER lane rests a limit AT the level and fills only on a later touch.

The taker lane is therefore the control: it needs no fill assumption at all.
When live fills arrive, comparing realised maker fills against this lane's
signals measures the assumption instead of trusting it.

RESEARCH_ONLY.  Neither lane is capital-eligible; both are measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from vnedge.execution.exit_engine import ExitConfig, ExitEngine
from vnedge.execution.trigger_engine import TriggerConfig, TriggerEngine
from vnedge.runtime.scanner_session import ScannerSession, SessionCosts
from vnedge.strategy.arm_sources import StructureBounceArmSource
from vnedge.strategy.production_filters import ProductionGate

#: Shared across both lanes.  Changing anything here changes BOTH, which is
#: the point: the pair is only interpretable while they differ in one plane.
SHARED_ARM = {"map_timeframe_mult": 48, "min_confidence": 50}
SHARED_GATE = {"use_stoch_obv": True}
SHARED_TRIGGER = {
    "atr_stop_mult": 2.5,
    "stop_pct_floor": 0.0055,
    "stop_pct_cap": 0.0095,
    "vol_mult": 0.0,  # soft volume: the scanner scores it, the trigger does not gate
}
SHARED_EXIT = {
    "failed_breakout": False,
    # None, not 999: the runtime engine has no no-progress rule at all, and a
    # sentinel that merely never fires still says the lane owns one.
    "no_progress_bars": None,
    "absolute_max_bars": 288,
    "tp_ladder": ((1.5, 0.4), (2.5, 0.3), (4.0, 0.3)),
    "breakeven_after_tp1": True,
}


@dataclass(frozen=True, slots=True)
class BounceLane:
    """One lane: everything shared, plus its own entry and fee plane."""

    lane_id: str
    entry_mode: str
    maker_bps: float | None = None
    retest_expiry_bars: int = 6
    strategy_id: str = "structure_bounce_prod_v1"

    def __post_init__(self) -> None:
        if self.entry_mode not in ("close", "retest_limit"):
            raise ValueError("entry_mode must be 'close' or 'retest_limit'")
        if self.entry_mode == "close" and self.maker_bps is not None:
            raise ValueError(
                "a close entry crosses the spread; it cannot book a maker fee"
            )
        if self.maker_bps is not None and self.maker_bps < 0:
            raise ValueError("maker_bps cannot be negative")

    @property
    def assumes_passive_fill(self) -> bool:
        """True when this lane's result depends on an unverified fill model."""
        return self.entry_mode == "retest_limit"

    def costs(self) -> SessionCosts:
        return SessionCosts(maker_bps=self.maker_bps)

    def trigger(self) -> TriggerEngine:
        return TriggerEngine(
            config=TriggerConfig(
                **SHARED_TRIGGER,
                entry_mode=self.entry_mode,
                retest_expiry_bars=self.retest_expiry_bars,
            )
        )

    def exits(self) -> ExitEngine:
        return ExitEngine(config=ExitConfig(**SHARED_EXIT))

    def arm_source(self) -> ProductionGate:
        return ProductionGate(
            inner=StructureBounceArmSource(**SHARED_ARM), **SHARED_GATE
        )

    def session(self, symbol: str, **kwargs) -> ScannerSession:
        """A session wired for this lane.  Both lanes share every other plane."""
        return ScannerSession(
            symbol=symbol,
            arm_source=self.arm_source(),
            trigger=self.trigger(),
            exits=self.exits(),
            costs=self.costs(),
            **kwargs,
        )


#: Control lane: crosses the spread, assumes nothing about passive fills.
TAKER_LANE = BounceLane(lane_id="structure_bounce_taker", entry_mode="close")

#: Test lane: rests at the level.  ``maker_bps`` is an ASSUMPTION under test,
#: not a measured venue rate -- the pair exists to settle it.
MAKER_LANE = BounceLane(
    lane_id="structure_bounce_maker", entry_mode="retest_limit", maker_bps=2.0
)

LANES: tuple[BounceLane, ...] = (TAKER_LANE, MAKER_LANE)


def lane_by_id(lane_id: str) -> BounceLane:
    for lane in LANES:
        if lane.lane_id == lane_id:
            return lane
    raise KeyError(f"unknown bounce lane: {lane_id}")


def maker_lane_at(maker_bps: float) -> BounceLane:
    """The maker lane at a different assumed fee -- for sensitivity, not tuning."""
    return replace(MAKER_LANE, maker_bps=maker_bps)
