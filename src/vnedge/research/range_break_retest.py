"""Closed-bar scalp hypothesis; research only, deliberately not registered.

Preserves the analyze/detect/score/veto/signal shape without importing the
external bot or pretending heuristic quality is an empirical edge estimate.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from typing import Literal

import numpy as np
import pandas as pd

from vnedge.plan.cost_model import CostModel
from vnedge.strategy.arm_evidence import FrozenPermissionSnapshot, assert_decision_row
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent, bind_signal_decision

Side = Literal["long", "short"]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class RetestSpec:
    strategy_id: str = "range_break_retest_5m_v1"
    exchange: str = "delta_india"
    symbols: tuple[str, ...] = ("BTCUSD", "ETHUSD")
    timeframe: str = "5m"
    entry_clock: str = "next_5m_open"
    context_timeframes: tuple[str, ...] = ()
    range_bars: int = 20
    min_breakout_body_fraction: float = 0.60
    max_extension_fraction: float = 0.25
    retest_tolerance_fraction: float = 0.05
    min_close_location: float = 0.60
    stop_pad_fraction: float = 0.05
    min_room_cost_multiple: float = 2.0
    min_net_reward_risk: float = 1.5
    max_hold_bars: int = 3
    cost_profile_id: str = "delta_scalp_v2"
    cost_config_sha256: str = "e04d112e35708157a77fc5b5721e4e2c4b2c0ce6d05c71b65bff3bf443e7b13a"
    booked_round_bps: float = 17.8


SPEC = RetestSpec()


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    points: float
    maximum: float


@dataclass(frozen=True)
class ResearchCandidate:
    signal: SignalIntent
    evidence_id: str
    episode_id: str
    spec_sha256: str
    input_bar_hashes: tuple[str, ...]
    range_low: float
    range_high: float
    broken_level: float
    breakout_open: str
    reference_entry: float
    target_room_bps: float
    stop_distance_bps: float
    net_reward_risk: float
    score_components: tuple[ScoreComponent, ...]
    cost_profile_id: str
    cost_config_sha256: str
    booked_round_bps: float
    path_id: str = field(default="research_observe", init=False)
    can_trade: bool = field(default=False, init=False)
    can_promote: bool = field(default=False, init=False)
    performance_eligible: bool = field(default=False, init=False)

    @property
    def quality_score(self) -> float:
        return sum(item.points for item in self.score_components)


@dataclass(frozen=True)
class RetestEvaluation:
    candidate: ResearchCandidate | None
    failed_gates: tuple[str, ...]
    score_components: tuple[ScoreComponent, ...] = ()

    def diagnostics(self) -> dict[str, object]:
        candidate = self.candidate
        envelope = candidate.signal.decision_envelope if candidate else None
        return {
            "strategy_id": SPEC.strategy_id,
            "eligible": candidate is not None,
            "primary_failed_gate": self.failed_gates[0] if self.failed_gates else None,
            "failed_gates": list(self.failed_gates),
            "quality_score": sum(item.points for item in self.score_components),
            "score_kind": "geometry_rank_not_probability",
            "score_components": [asdict(item) for item in self.score_components],
            "decision_id": envelope.decision_id if envelope else None,
            "snapshot_id": envelope.snapshot_id if envelope else None,
            "episode_id": candidate.episode_id if candidate else None,
            "evidence_id": candidate.evidence_id if candidate else None,
            "expected_gross_edge_bps": None,
            "execution_blockers": ["research_only", "oos_edge_unproven", "execution_parity_unproven"],
            "path_id": "research_observe",
            "can_trade": False, "can_promote": False, "performance_eligible": False,
        }


class RangeBreakRetestResearch(BaseStrategy):
    """One fixed revision, one symbol per instance, pure prefix evaluation.

    ``analyze`` offers the external-style list-of-signals interface. Use
    ``evaluate`` for the immutable score/evidence wrapper and rejection list.
    There is no registry entry, network, model, book, or adaptive state here.
    """

    strategy_id = SPEC.strategy_id
    warmup_bars = SPEC.range_bars + 2

    def __init__(self, symbol: str) -> None:
        if symbol not in SPEC.symbols:
            raise ValueError("research universe is frozen to BTCUSD / ETHUSD")
        self.symbol = symbol

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        # No full-history indicator rebuild. All consumed features are derived
        # from the validated 22-row prefix window at evaluation time.
        return candles.copy(deep=True)

    def get_required_timeframes(self) -> list[str]:
        return [SPEC.timeframe]

    def analyze(self, symbol: str, candles_dict: Mapping[str, pd.DataFrame]) -> list[SignalIntent]:
        candidate = self.evaluate_market(symbol, candles_dict).candidate
        return [candidate.signal] if candidate else []

    def evaluate_market(self, symbol: str, candles_dict: Mapping[str, pd.DataFrame]) -> RetestEvaluation:
        """Stateless status and output from the same evaluation; no stale status cache."""
        if symbol != self.symbol:
            raise ValueError("symbol does not match this isolated research lane")
        frame = candles_dict.get(SPEC.timeframe)
        if frame is None or frame.empty:
            return RetestEvaluation(None, ("decision_frame_missing",))
        return self.evaluate(frame, len(frame) - 1)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        candidate = self.evaluate(df, index).candidate
        return candidate.signal if candidate else None

    def evaluation_diagnostics(self, df: pd.DataFrame, index: int) -> dict[str, object]:
        return self.evaluate(df, index).diagnostics()

    def evaluate(self, df: pd.DataFrame, index: int) -> RetestEvaluation:
        if not 0 <= index < len(df):
            raise IndexError("decision index outside frame")
        if not df.columns.is_unique:
            return RetestEvaluation(None, ("duplicate_input_columns",))
        if index + 1 < self.warmup_bars:
            return RetestEvaluation(None, ("warmup_incomplete",))
        window = df.iloc[index + 1 - self.warmup_bars:index + 1]
        failures: list[str] = []
        opens: list[pd.Timestamp] = []
        hashes: list[str] = []
        for row in window.to_dict("records"):
            for key, expected in (("symbol", self.symbol), ("exchange", SPEC.exchange),
                                  ("timeframe", SPEC.timeframe)):
                if str(row.get(key)) != expected:
                    failures.append(f"{key}_identity_mismatch")
            coverage = row.get("coverage_ok")
            if not isinstance(coverage, (bool, np.bool_)) or not coverage:
                failures.append("coverage_not_ok")
            for key in ("open", "high", "low", "close"):
                try:
                    number = float(row.get(key, float("nan")))
                    if not math.isfinite(number) or number <= 0:
                        failures.append(f"{key}_not_finite_positive")
                except (ValueError, TypeError, OverflowError):
                    failures.append(f"{key}_not_finite_positive")
            try:
                volume = float(row.get("volume", float("nan")))
                if not math.isfinite(volume) or volume <= 0:
                    failures.append("positive_base_volume_required")
            except (ValueError, TypeError):
                failures.append("positive_base_volume_required")
            try:
                raw_time = pd.Timestamp(row.get("timestamp"))
                if pd.isna(raw_time) or raw_time.tzinfo is None:
                    failures.append("utc_timestamp_required")
                ref = assert_decision_row(row, timeframe=SPEC.timeframe)
                opens.append(pd.Timestamp(ref.open_time))
                hashes.append(str(ref.content_sha256))
            except (ValueError, TypeError, KeyError) as exc:
                failures.append(str(exc))
        if len(opens) == len(window) and any(
            b - a != pd.Timedelta(minutes=5) for a, b in pairwise(opens)
        ):
            failures.append("decision_window_gap_or_duplicate")
        cost = CostModel.for_profile(SPEC.cost_profile_id)
        if cost.config_sha256 != SPEC.cost_config_sha256:
            failures.append("cost_contract_changed")
        if failures:
            return RetestEvaluation(None, tuple(dict.fromkeys(failures)))

        prior, breakout, retest = window.iloc[:-2], window.iloc[-2], window.iloc[-1]
        high, low = float(prior.high.astype(float).max()), float(prior.low.astype(float).min())
        width = high - low
        if width <= 0:
            return RetestEvaluation(None, ("prior_range_empty",))
        bo, bc = float(breakout.open), float(breakout.close)
        if bo <= high < bc:
            side: Side = "long"
            direction, level = 1, high
        elif bc < low <= bo:
            side = "short"
            direction, level = -1, low
        else:
            return RetestEvaluation(None, ("no_fresh_range_break",))

        bh, bl = float(breakout.high), float(breakout.low)
        body_fraction = abs(bc - bo) / (bh - bl) if bh > bl else 0.0
        if body_fraction < SPEC.min_breakout_body_fraction:
            failures.append("breakout_body_too_small")
        extension = direction * (bc - level) / width
        if extension > SPEC.max_extension_fraction:
            failures.append("breakout_overextended")
        ro, rh, rl, rc = (float(retest[k]) for k in ("open", "high", "low", "close"))
        touch = rl if side == "long" else rh
        if abs(touch - level) > SPEC.retest_tolerance_fraction * width:
            failures.append("boundary_retest_missing")
        if direction * (rc - level) <= 0 or direction * (rc - ro) <= 0:
            failures.append("retest_hold_failed")
        location = ((rc - rl) if side == "long" else (rh - rc)) / (rh - rl) if rh > rl else 0.0
        if location < SPEC.min_close_location:
            failures.append("retest_close_location_weak")
        if direction * (rc - level) > SPEC.max_extension_fraction * width:
            failures.append("retest_entry_overextended")
        stop = touch - direction * SPEC.stop_pad_fraction * width
        target = level + direction * width
        risk_bps, room_bps = direction * (rc - stop) / rc * 10000, direction * (target - rc) / rc * 10000
        if not all(math.isfinite(value) for value in (stop, target, risk_bps, room_bps)):
            return RetestEvaluation(None, tuple(dict.fromkeys([*failures, "nonfinite_geometry"])))
        if min(stop, target) <= 0 or risk_bps <= 0 or room_bps <= 0:
            failures.append("invalid_stop_target_geometry")
        booked = cost.round_trip_bps(include_safety=False)
        net_rr = (room_bps - booked) / (risk_bps + booked) if risk_bps > 0 else 0.0
        if room_bps < SPEC.min_room_cost_multiple * booked:
            failures.append("target_room_below_cost_floor")
        if net_rr < SPEC.min_net_reward_risk:
            failures.append("net_reward_risk_too_small")
        scores = (
            ScoreComponent("breakout_extension", 30 * min(1.0, max(0.0, extension / SPEC.max_extension_fraction)), 30),
            ScoreComponent("retest_close_location", 40 * min(1.0, max(0.0, location)), 40),
            ScoreComponent("net_reward_risk", 30 * min(1.0, max(0.0, net_rr / 3)), 30),
        )
        if failures:
            return RetestEvaluation(None, tuple(dict.fromkeys(failures)), scores)
        spec_hash = _digest(asdict(SPEC))
        episode_id = _digest((self.strategy_id, self.symbol, side, hashes[-2]))
        evidence_id = _digest((spec_hash, self.symbol, side, hashes))
        reason = f"{self.strategy_id} episode={episode_id} evidence={evidence_id}"
        # Never inherit mreg_* / bos_* annotations from a caller's prepared
        # frame: this hypothesis declares no HTF permission plane.
        permission = FrozenPermissionSnapshot(
            decision_bar=assert_decision_row(retest.to_dict(), timeframe=SPEC.timeframe),
            context_bars=(), allow_long=side == "long", allow_short=side == "short",
            regime_state="not_applicable", direction="not_applicable", reason=reason,
            regime_version=self.strategy_id,
        )
        intent = bind_signal_decision(
            SignalIntent(side=side, stop_price=stop, take_profit_price=target,
                         reason=reason, permission_snapshot=permission),
            strategy_id=self.strategy_id, symbol=self.symbol, timeframe=SPEC.timeframe,
            decision_row=retest.to_dict(), entry_clock=SPEC.entry_clock,
            require_canonical_truth=True, require_existing_snapshot=True,
        )
        candidate = ResearchCandidate(
            signal=intent, evidence_id=evidence_id, episode_id=episode_id,
            spec_sha256=spec_hash, input_bar_hashes=tuple(hashes), range_low=low,
            range_high=high, broken_level=level, breakout_open=opens[-2].isoformat(),
            reference_entry=rc, target_room_bps=room_bps, stop_distance_bps=risk_bps,
            net_reward_risk=net_rr, score_components=scores, cost_profile_id=cost.profile,
            cost_config_sha256=cost.config_sha256, booked_round_bps=booked,
        )
        return RetestEvaluation(candidate, (), scores)
