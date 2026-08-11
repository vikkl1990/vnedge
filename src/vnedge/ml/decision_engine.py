"""Decision Engine — turns model outputs into an ``ArmIntent``, nothing more.

The engine is deliberately thin and *decoupled from order placement*: it loads
only registry-approved models, scores the current feature row, applies hard
thresholds, and emits an ``ArmIntent`` (or ``None``). Converting that intent
into a ``SignalIntent`` and running it through ``PreTradeRiskGateway`` happens
downstream — the gateway/kill-switch are untouched and keep final authority.

The model contract is one method: ``score(features) -> dict[str, float]`` with
keys drawn from ``{"long", "short", "edge_bps", "confidence", "invalidation"}``.
``MockModel`` is the reference implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Mapping, Protocol, runtime_checkable

import pandas as pd

from vnedge.ml.model_registry import ModelRegistry
from vnedge.strategy.base_strategy import SignalIntent


@runtime_checkable
class ScoringModel(Protocol):
    """Anything the Decision Engine can score with."""

    def score(self, features: Mapping[str, float]) -> dict[str, float]: ...


@dataclass(frozen=True)
class MockModel:
    """Reference/test model: returns fixed scores regardless of input."""

    scores: dict[str, float]

    def score(self, features: Mapping[str, float]) -> dict[str, float]:
        return dict(self.scores)


@dataclass(frozen=True)
class ModelPrediction:
    model_id: str
    timestamp: datetime
    scores: dict[str, float]
    confidence: float
    feature_hash: str
    extras: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ArmIntent:
    side: Literal["long", "short"] | None
    strength: float                       # 0–1 size multiplier
    probability: float
    expected_edge_bps: float | None
    invalidation_price: float | None
    model_id: str
    reason: str
    expires_after_bars: int | None = None


@dataclass(frozen=True)
class DecisionEngineConfig:
    min_probability: float = 0.60
    min_confidence: float = 0.50
    min_expected_edge_bps: float = 20.0
    max_strength: float = 1.0
    combination_mode: Literal["single", "average", "max"] = "single"
    require_cost_clear: bool = True
    expires_after_bars: int | None = None


def _feature_hash(features: Mapping[str, float]) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps({k: float(v) for k, v in sorted(features.items())}, sort_keys=True).encode()
    ).hexdigest()[:16]


class DecisionEngine:
    """Loads approved models and emits ``ArmIntent`` from a feature row."""

    def __init__(
        self,
        registry: ModelRegistry,
        active_model_ids: list[str],
        config: DecisionEngineConfig | None = None,
    ) -> None:
        self.registry = registry
        self.config = config or DecisionEngineConfig()
        # load_artifact() raises for any model not in {'paper','promoted'} — the
        # engine can never run a research/candidate model.
        self.models: dict[str, ScoringModel] = {
            mid: registry.load_artifact(mid) for mid in active_model_ids
        }
        self.active_model_ids = list(active_model_ids)

    def predict(self, features: Mapping[str, float]) -> list[ModelPrediction]:
        fh = _feature_hash(features)
        now = datetime.now(UTC)
        preds: list[ModelPrediction] = []
        for mid, model in self.models.items():
            s = model.score(features)
            preds.append(ModelPrediction(
                model_id=mid, timestamp=now, scores=dict(s),
                confidence=float(s.get("confidence", 1.0)), feature_hash=fh,
            ))
        return preds

    def _combine(self, preds: list[ModelPrediction]) -> dict[str, float]:
        if not preds:
            return {}
        keys = set().union(*(p.scores.keys() for p in preds))
        mode = self.config.combination_mode
        if mode == "single":
            return dict(preds[0].scores)
        out: dict[str, float] = {}
        for k in keys:
            vals = [p.scores[k] for p in preds if k in p.scores]
            out[k] = (sum(vals) / len(vals)) if mode == "average" else max(vals)
        return out

    def decide(self, features: Mapping[str, float]) -> ArmIntent | None:
        """Score → threshold → ArmIntent (or None). Never places an order."""
        preds = self.predict(features)
        if not preds:
            return None
        s = self._combine(preds)
        p_long = float(s.get("long", 0.0))
        p_short = float(s.get("short", 0.0))
        side: Literal["long", "short"] | None = "long" if p_long >= p_short else "short"
        prob = p_long if side == "long" else p_short
        conf = float(s.get("confidence", min(p.confidence for p in preds)))
        edge = s.get("edge_bps")
        cfg = self.config

        if prob < cfg.min_probability:
            return None
        if conf < cfg.min_confidence:
            return None
        if edge is not None and float(edge) < cfg.min_expected_edge_bps:
            return None
        if cfg.require_cost_clear and edge is None:
            return None

        strength = min(prob, cfg.max_strength)
        model_id = preds[0].model_id if cfg.combination_mode == "single" else "+".join(self.active_model_ids)
        return ArmIntent(
            side=side, strength=strength, probability=prob,
            expected_edge_bps=float(edge) if edge is not None else None,
            invalidation_price=s.get("invalidation"),
            model_id=model_id,
            reason=f"decision_engine {side} p={prob:.3f} conf={conf:.2f}"
                   + (f" edge={float(edge):.0f}bps" if edge is not None else ""),
            expires_after_bars=cfg.expires_after_bars,
        )


def arm_to_signal_intent(
    arm: ArmIntent, *, close: float, atr: float, stop_atr_mult: float = 1.5,
    target_r_mult: float = 2.0,
) -> SignalIntent:
    """Adapter: ArmIntent → SignalIntent for the EXISTING risk-gateway path.

    Attaches the mandatory stop + target. Does not touch the gateway; the caller
    submits the returned SignalIntent through the normal pipeline.
    """
    if arm.side is None:
        raise ValueError("cannot build a SignalIntent from a flat ArmIntent")
    stop_dist = stop_atr_mult * atr
    if arm.side == "long":
        stop, target = close - stop_dist, close + target_r_mult * stop_dist
    else:
        stop, target = close + stop_dist, close - target_r_mult * stop_dist
    return SignalIntent(
        side=arm.side, stop_price=max(stop, 1e-9),
        take_profit_price=max(target, 1e-9), reason=arm.reason,
    )
