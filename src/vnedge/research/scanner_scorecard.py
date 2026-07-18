"""Scanner scorecard + auto-suppression — evidence-driven lane weighting.

VNEDGE runs many scanner lanes with no per-signal outcome accounting, so a lane
with proven negative expectancy keeps firing forever. This module ports the
discipline from the operator's first bot (crypto-trading-bot/scanner_weights):
score every scanner by realised R-expectancy and AUTO-SUPPRESS the losers, with
a minimum-sample gate so a scanner is never judged on noise, and a recovery
path so a suppressed scanner can earn its way back on shadow evidence.

It decides STATUS ONLY. It never trades, never promotes, and holds no bypass:
suppression can lower a lane's weight to zero, but a live order still passes
every gate. Expectancy is supplied by the 30-min scalp outcome evaluator
(entry-fee-adjusted net R), never self-reported by the scanner.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Mapping

# --- thresholds (ported; expectancy is NET R after the entry fee) -----------
MIN_SAMPLES = 8               # never judge a scanner on fewer closed outcomes
SUPPRESS_EXPECTANCY = -0.05   # net-R below this -> suppress (weight 0)
REDUCE_EXPECTANCY = 0.02      # net-R below this (but >= suppress) -> reduced
REDUCED_WEIGHT = 0.5
# recovery: a suppressed scanner needs sustained positive shadow evidence
RECOVERY_MIN_SAMPLES = 20
RECOVERY_EXPECTANCY = 0.05

STATUS_ACTIVE = "active"
STATUS_REDUCED = "reduced"
STATUS_SUPPRESSED = "suppressed"
STATUS_LEARNING = "learning"


@dataclass(frozen=True)
class ScannerStat:
    scanner_id: str
    status: str
    weight: float
    expectancy_r: float
    samples: int
    reason: str


def classify(expectancy_r: float, samples: int, *, was_suppressed: bool = False) -> ScannerStat:
    """Pure decision: (net-R expectancy, sample count) -> status + weight.

    Deny-by-default on thin evidence: under MIN_SAMPLES a scanner is LEARNING
    (kept observable, not trusted). A suppressed scanner only recovers on a
    larger sample AND clearly positive expectancy — noise cannot un-suppress it.
    """
    if samples < MIN_SAMPLES:
        return ScannerStat("", STATUS_LEARNING, 0.0, expectancy_r, samples,
                           f"learning ({samples}/{MIN_SAMPLES} outcomes)")
    if was_suppressed:
        if samples >= RECOVERY_MIN_SAMPLES and expectancy_r >= RECOVERY_EXPECTANCY:
            return ScannerStat("", STATUS_ACTIVE, 1.0, expectancy_r, samples,
                               f"recovered: +{expectancy_r:.3f}R over {samples}")
        return ScannerStat("", STATUS_SUPPRESSED, 0.0, expectancy_r, samples,
                           f"suppressed; recovery needs >={RECOVERY_EXPECTANCY}R over "
                           f">={RECOVERY_MIN_SAMPLES} (have {expectancy_r:+.3f}R/{samples})")
    if expectancy_r < SUPPRESS_EXPECTANCY:
        return ScannerStat("", STATUS_SUPPRESSED, 0.0, expectancy_r, samples,
                           f"negative edge {expectancy_r:+.3f}R over {samples}")
    if expectancy_r < REDUCE_EXPECTANCY:
        return ScannerStat("", STATUS_REDUCED, REDUCED_WEIGHT, expectancy_r, samples,
                           f"thin edge {expectancy_r:+.3f}R -> half weight")
    return ScannerStat("", STATUS_ACTIVE, 1.0, expectancy_r, samples,
                       f"positive edge {expectancy_r:+.3f}R over {samples}")


class ScannerScorecard:
    """Persistent per-scanner status. Consumes outcome-evaluator expectancy."""

    def __init__(self, store_path: Path | None = None) -> None:
        self._store = store_path
        self._prev_suppressed: set[str] = set()
        if store_path and store_path.exists():
            try:
                data = json.loads(store_path.read_text())
                self._prev_suppressed = {
                    k for k, v in data.items() if v.get("status") == STATUS_SUPPRESSED
                }
            except (json.JSONDecodeError, OSError):
                self._prev_suppressed = set()

    def evaluate(self, outcomes: Mapping[str, tuple[float, int]]) -> dict[str, ScannerStat]:
        """outcomes: {scanner_id: (net_expectancy_r, samples)} -> per-scanner stat."""
        result: dict[str, ScannerStat] = {}
        for sid, (exp, n) in outcomes.items():
            stat = classify(exp, n, was_suppressed=sid in self._prev_suppressed)
            result[sid] = ScannerStat(sid, stat.status, stat.weight, stat.expectancy_r,
                                      stat.samples, stat.reason)
        if self._store is not None:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            self._store.write_text(json.dumps(
                {k: asdict(v) for k, v in result.items()}, indent=2))
        return result

    @staticmethod
    def suppressed(stats: Mapping[str, ScannerStat]) -> list[str]:
        return sorted(k for k, v in stats.items() if v.status == STATUS_SUPPRESSED)
