"""regime_v0 — the frozen, causal, rules-based regime model (Model v0).

A *control layer*, not an alpha signal: it classifies each closed bar into
trend_up / trend_down / chop / high_vol (or unknown during warm-up) and emits
allow-flags + a confidence a strategy can use as a hard filter or a soft size
multiplier. It is built ONLY from the existing causal primitives in
``strategy/regime.py`` (efficiency ratio, ATR percentile, EMA-alignment trend
flags) — no new features, thresholds frozen (see docs/prereg/regime_v0_*).

It is Registry-versioned so an HMM v1 must beat this baseline OOS through the
same promotion machinery before it can displace it (CLAUDE.md rule). Nothing
here trades or bypasses the gateway; it only narrows/sizes what a strategy may
already do.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import pandas as pd

from vnedge.strategy.regime import (
    RegimeParams, add_regime_columns, regime_warmup_bars,
)

MODEL_ID = "regime_v0_20260812"
FEATURE_SET_VERSION = "regime.add_regime_columns.v1"

Label = str  # trend_up | trend_down | chop | high_vol | unknown


@dataclass(frozen=True)
class RegimeV0Params:
    regime: RegimeParams = field(default_factory=RegimeParams)
    high_vol_pct: float = 0.90   # atr_pct at/above this = high_vol (dominant state)


@dataclass(frozen=True)
class RegimeReading:
    label: Label
    allow_long: bool
    allow_short: bool
    confidence: float
    scores: dict
    features_used: tuple[str, ...]
    model_id: str = MODEL_ID

    def to_dict(self) -> dict:
        d = asdict(self)
        d["features_used"] = list(self.features_used)
        return d


def _clip01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return max(0.0, min(1.0, x))


class RegimeV0:
    """Frozen rules regime model. ``classify`` is causal: bar ``index`` reads
    only features computed from data up to and including ``index``."""

    model_id = MODEL_ID
    family = "regime"
    _FEATURES = ("er", "atr_pct", "regime_trend_up", "regime_trend_down")

    def __init__(self, params: RegimeV0Params | None = None) -> None:
        self.params = params or RegimeV0Params()

    @property
    def warmup_bars(self) -> int:
        return regime_warmup_bars(self.params.regime)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return add_regime_columns(candles, self.params.regime)

    def classify(self, candles: pd.DataFrame, index: int) -> RegimeReading:
        return self.read_row(self.prepare(candles).iloc[index])

    def read_row(self, row: pd.Series) -> RegimeReading:
        er = float(row.get("er", float("nan")))
        atr_pct = float(row.get("atr_pct", float("nan")))
        trend_up = bool(row.get("regime_trend_up", False))
        trend_down = bool(row.get("regime_trend_down", False))

        # warm-up: any core feature NaN -> unknown, neutral pass-through (the
        # strategy's warmup_bars gate handles insufficient data, not regime_v0).
        if math.isnan(er) or math.isnan(atr_pct):
            return self._reading("unknown", True, True, 0.0,
                                 {"trend_up": 0.0, "trend_down": 0.0, "chop": 0.0, "high_vol": 0.0})

        high_vol = atr_pct >= self.params.high_vol_pct
        scores = {
            "high_vol": _clip01(atr_pct),
            "trend_up": _clip01(er) if trend_up else 0.0,
            "trend_down": _clip01(er) if trend_down else 0.0,
            "chop": _clip01(1.0 - er) if not (trend_up or trend_down) else 0.0,
        }
        if high_vol:  # dominant: stand down regardless of direction
            return self._reading("high_vol", False, False, _clip01(atr_pct), scores)
        if trend_up:
            return self._reading("trend_up", True, False, _clip01(er), scores)
        if trend_down:
            return self._reading("trend_down", False, True, _clip01(er), scores)
        return self._reading("chop", True, True, _clip01(1.0 - er), scores)

    def _reading(self, label, allow_long, allow_short, confidence, scores) -> RegimeReading:
        return RegimeReading(
            label=label, allow_long=allow_long, allow_short=allow_short,
            confidence=round(confidence, 4), scores={k: round(v, 4) for k, v in scores.items()},
            features_used=self._FEATURES,
        )

    # --- Registry integration (rules-as-model) --------------------------------
    def config_hash(self) -> str:
        payload = json.dumps({"params": asdict(self.params), "features": FEATURE_SET_VERSION},
                             sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()[:16]

    def artifact_bytes(self) -> bytes:
        """The 'artifact' for a rules model is its frozen config (joblib-loadable)."""
        import io
        import joblib
        buf = io.BytesIO()
        joblib.dump({"model_id": self.model_id, "params": asdict(self.params),
                     "feature_set_version": FEATURE_SET_VERSION}, buf)
        return buf.getvalue()

    def metadata(self, created_at: datetime | None = None):
        """ModelMetadata for the Registry (status='research'; promote via the ladder)."""
        from vnedge.ml.model_registry import ModelMetadata
        return ModelMetadata(
            model_id=self.model_id, family=self.family,
            created_at=created_at or datetime.now(UTC),
            trained_on_window="n/a (rules)", feature_set_version=FEATURE_SET_VERSION,
            algorithm="rules", hyperparams=asdict(self.params), metrics={},
            status="research", artifact_path=f"{self.model_id}.joblib",
            config_hash=self.config_hash(),
            notes="Frozen rules regime baseline; HMM v1 must beat OOS to displace.",
        )
