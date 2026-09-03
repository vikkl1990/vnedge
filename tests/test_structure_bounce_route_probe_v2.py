from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from vnedge.execution.trigger_engine import ArmState
from vnedge.strategy.structure_bounce_route_probe_v2 import StructureBounceRouteProbeV2


class _OneCandidateGate:
    def __init__(self) -> None:
        self.blocked: dict[str, int] = {}
        self.inner = SimpleNamespace(last_confidence=74)
        self.last_regime = SimpleNamespace(label="range")

    def observe(self, ctx):
        if ctx.index != 60:
            return None
        return ArmState(
            episode_id=1,
            box_high=100.2,
            box_low=99.81,
            compressed=True,
            atr=ctx.atr,
            vol_ma=ctx.vol_ma,
            prev_close=ctx.prev_close,
            side_hint="long",
        )


class _FastProbe(StructureBounceRouteProbeV2):
    warmup_bars = 50

    @staticmethod
    def _new_gate():
        return _OneCandidateGate()


def _frame(rows: int = 80) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC"),
            "open": [100.0] * rows,
            "high": [100.5] * rows,
            "low": [99.5] * rows,
            "close": [100.0] * rows,
            "volume": [10.0] * rows,
            "data_quality": ["ok"] * rows,
        }
    )


def test_probe_emits_route_neutral_level_stop_and_target() -> None:
    strategy = _FastProbe()
    first = strategy.prepare(_frame())
    second = strategy.prepare(_frame())

    pd.testing.assert_series_equal(first["sbrp_side"], second["sbrp_side"])
    intent = strategy.signal(first, 60)
    assert intent is not None
    assert intent.side == "long"
    assert intent.entry_limit_price == pytest.approx(99.81)
    assert intent.stop_price < intent.entry_limit_price < float(first.iloc[60]["close"])
    assert intent.take_profit_price is not None
    assert intent.take_profit_price > float(first.iloc[60]["close"])
    assert first.iloc[60]["sbrp_status"] == "candidate"


def test_probe_never_emits_on_bad_quality() -> None:
    frame = _frame()
    frame.loc[60, "data_quality"] = "gap"
    prepared = _FastProbe().prepare(frame)
    assert _FastProbe().signal(prepared, 60) is None
    assert prepared.iloc[60]["sbrp_status"] == "data_quality_not_ok"
