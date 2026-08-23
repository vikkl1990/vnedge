"""MFE capture: what fraction of its best moment did a trade actually bank?

    capture = gross_bps / mfe_bps

The ratio is a realism check on the EXIT, not a performance metric. An exit
finer than the bar it is measured on cannot be adjudicated from OHLC -- the
backtester silently resolves the intrabar path favourably, and the book banks
almost the whole maximum favourable excursion on almost every trade.

Measured 2026-08-23 on a public 12-strategy catalogue whose every entry used an
ATR trailing stop ~1/65th the width of its bar: median capture 93.9%, with
49.6% of trades banking over 95% of their best price. Those results are
unreachable live, and the ratio is what exposed them.

Reference bands, from that measurement and ordinary exit behaviour:

    > 0.90   implausible -- suspect an exit finer than the data's resolution
    0.55-0.90 tight but achievable, e.g. a real trailing stop
    0.25-0.55 typical of target or signal exits
    < 0.25   the exit is giving most of the move back

A LOW ratio is not a defect; it is the honest cost of exiting on a rule. Only
the high end indicts the measurement.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Sequence

#: Above this, an exit is banking more of its best moment than bar data can
#: justify. Not a hard proof of error -- a threshold for looking.
IMPLAUSIBLE_CAPTURE = 0.90


@dataclass(frozen=True, slots=True)
class CaptureReport:
    n: int
    scored: int
    median: float
    mean: float
    p90: float
    share_above_threshold: float
    verdict: str

    @property
    def implausible(self) -> bool:
        return self.verdict == "implausible"


def capture_ratios(trades: Sequence) -> list[float]:
    """gross / MFE for every trade that reached a favourable excursion.

    Trades that never moved in favour are skipped rather than scored zero:
    dividing by a zero best-moment says nothing about the exit.
    """
    out: list[float] = []
    for trade in trades:
        mfe = getattr(trade, "mfe_bps", 0.0) or 0.0
        if mfe <= 0:
            continue
        out.append(getattr(trade, "gross_bps", 0.0) / mfe)
    return out


def score(trades: Sequence, *, threshold: float = IMPLAUSIBLE_CAPTURE) -> CaptureReport:
    ratios = sorted(capture_ratios(trades))
    if not ratios:
        return CaptureReport(len(trades), 0, float("nan"), float("nan"),
                             float("nan"), 0.0, "no_excursions")
    above = sum(1 for r in ratios if r > threshold) / len(ratios)
    median = statistics.median(ratios)
    verdict = (
        "implausible" if median > threshold
        else "tight" if median > 0.55
        else "typical" if median > 0.25
        else "gives_it_back"
    )
    return CaptureReport(
        n=len(trades),
        scored=len(ratios),
        median=median,
        mean=statistics.fmean(ratios),
        p90=ratios[int(0.9 * (len(ratios) - 1))],
        share_above_threshold=above,
        verdict=verdict,
    )
