from __future__ import annotations

import pandas as pd

from vnedge.strategy.range_expansion_observer_v3 import RangeExpansionObserverV3
from vnedge.strategy.range_expansion_observer_v4 import RangeExpansionObserverV4
from vnedge.strategy.scanner_spacing import apply_final_eligibility_spacing
from vnedge.strategy.structure_bos_15m_trigger_v2 import StructureBos15mTriggerV2
from vnedge.strategy.structure_bos_15m_trigger_v3 import StructureBos15mTriggerV3


def test_spacing_helper_does_not_consume_cooldown_for_rejected_setup() -> None:
    index = pd.RangeIndex(24)
    long_eligible = pd.Series(False, index=index)
    short_eligible = pd.Series(False, index=index)
    long_eligible.iloc[20] = True

    fire_long, fire_short, spacing_ok = apply_final_eligibility_spacing(
        long_eligible,
        short_eligible,
        min_bars_between_signals=48,
    )

    assert fire_long.iloc[20] == 1.0
    assert fire_short.sum() == 0.0
    assert spacing_ok.iloc[20] == 1.0


def _range_parent_frame() -> pd.DataFrame:
    out = pd.DataFrame(index=pd.RangeIndex(24))
    for name in (
        "rex3_quality_ok",
        "rex3_session_ok",
        "rex3_expansion_ok",
        "rex3_volume_ok",
    ):
        out[name] = 1.0
    out["rex3_body_bps"] = 20.0
    out["rex3_projected_net_bps"] = 30.0
    out["rex3_break_long"] = 0.0
    out["rex3_break_short"] = 0.0
    # A raw break at 10 fails final edge.  The valid break at 20 is inside the
    # 48-bar distance that V3 incorrectly measured from the rejected setup.
    out.loc[10, ["rex3_break_long", "rex3_projected_net_bps"]] = [1.0, 10.0]
    out.loc[20, "rex3_break_long"] = 1.0
    out["rex3_spacing_ok"] = 0.0
    out["rex3_fire_long"] = 0.0
    out["rex3_fire_short"] = 0.0
    return out


def test_range_v4_spaces_from_final_economic_eligibility(monkeypatch) -> None:
    parent = _range_parent_frame()
    monkeypatch.setattr(
        RangeExpansionObserverV3,
        "prepare",
        lambda self, candles: parent.copy(),
    )

    prepared = RangeExpansionObserverV4().prepare(pd.DataFrame())

    assert prepared.loc[10, "rex4_final_eligible_long"] == 0.0
    assert prepared.loc[10, "rex3_fire_long"] == 0.0
    assert prepared.loc[20, "rex4_final_eligible_long"] == 1.0
    assert prepared.loc[20, "rex3_spacing_ok"] == 1.0
    assert prepared.loc[20, "rex3_fire_long"] == 1.0


def _bos_parent_frame() -> pd.DataFrame:
    out = pd.DataFrame(index=pd.RangeIndex(24))
    out["bos15_structure_ready"] = True
    out["bos15_quality_ok"] = 1.0
    out["bos15_session_ok"] = 1.0
    out["bos15_volume_ok"] = 1.0
    out["bos15_structure_trend"] = "up"
    out["bos15_htf_structure_trend"] = "up"
    out["bos15_dual_avwap_bias"] = "between"
    out["bos15_break_long"] = 0.0
    out["bos15_break_short"] = 0.0
    out["bos15_projected_net_long_bps"] = 10.0
    out["bos15_projected_net_short_bps"] = 10.0
    out.loc[10, ["bos15_break_long", "bos15_projected_net_long_bps"]] = [1.0, 2.0]
    out.loc[20, "bos15_break_long"] = 1.0
    out["bos15_spacing_ok"] = 0.0
    out["bos15_fire_long"] = 0.0
    out["bos15_fire_short"] = 0.0
    return out


def test_bos_v3_spaces_from_direction_specific_final_edge(monkeypatch) -> None:
    parent = _bos_parent_frame()
    monkeypatch.setattr(
        StructureBos15mTriggerV2,
        "prepare",
        lambda self, candles: parent.copy(),
    )

    prepared = StructureBos15mTriggerV3().prepare(pd.DataFrame())

    assert prepared.loc[10, "bos15_v3_final_eligible_long"] == 0.0
    assert prepared.loc[10, "bos15_fire_long"] == 0.0
    assert prepared.loc[20, "bos15_v3_final_eligible_long"] == 1.0
    assert prepared.loc[20, "bos15_spacing_ok"] == 1.0
    assert prepared.loc[20, "bos15_fire_long"] == 1.0


def test_valid_setup_still_consumes_spacing() -> None:
    index = pd.RangeIndex(24)
    long_eligible = pd.Series(False, index=index)
    short_eligible = pd.Series(False, index=index)
    long_eligible.iloc[[10, 20]] = True

    fire_long, _, spacing_ok = apply_final_eligibility_spacing(
        long_eligible,
        short_eligible,
        min_bars_between_signals=48,
    )

    assert fire_long.iloc[10] == 1.0
    assert spacing_ok.iloc[20] == 0.0
    assert fire_long.iloc[20] == 0.0
