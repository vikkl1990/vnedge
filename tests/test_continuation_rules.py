"""Characterization tests for the V2 extraction, not new admission rules."""

from itertools import product

import numpy as np
import pandas as pd
import pytest

from vnedge.strategy.continuation_rules import assess_continuation, setup_masks
from vnedge.strategy.htf_regime_continuation_15m_v2 import HtfRegimeContinuation15mV2


def test_masks_match_frozen_formula_and_do_not_mutate() -> None:
    rng = np.random.default_rng(73)
    p = HtfRegimeContinuation15mV2().params
    size = 4096
    frame = pd.DataFrame({
        "bos15_structure_ready": rng.choice([0., 1., 2., np.nan], size),
        "bos15_quality_ok": rng.choice([0., 1., 2., np.nan], size),
        "bos15_structure_trend": rng.choice(["up", "down", "flat", "unknown"], size),
        "hsc_volume_ratio": rng.choice([0., p.volume_mult, 2., np.nan], size),
        "hsc_body_bps": rng.choice([0., p.min_reclaim_body_bps, 100., np.nan], size),
        **{f"hsc_pullback_{s}": rng.choice([0., 1., 2., np.nan], size)
           for s in ("long", "short")},
        **{f"hsc_projected_net_{s}_bps": rng.choice(
            [0., p.min_projected_net_bps, 100., np.nan], size)
           for s in ("long", "short")},
        **{f"mreg_allow_{s}": rng.choice([0., 1., 2., np.nan], size)
           for s in ("long", "short")},
    }, index=np.arange(size) * 3)
    original = frame.copy(deep=True)
    actual = setup_masks(frame, p)
    for side, direction, mask in zip(("long", "short"), ("up", "down"), actual):
        # Original V2 formula retained independently of the implementation.
        expected = (
            frame["bos15_structure_ready"].fillna(False).astype(bool)
            & frame["bos15_quality_ok"].eq(1)
            & frame["bos15_structure_trend"].astype(str).eq(direction)
            & pd.to_numeric(frame["hsc_volume_ratio"], errors="coerce").ge(p.volume_mult)
            & pd.to_numeric(frame["hsc_body_bps"], errors="coerce").ge(p.min_reclaim_body_bps)
            & frame[f"hsc_pullback_{side}"].eq(1)
            & pd.to_numeric(frame[f"hsc_projected_net_{side}_bps"], errors="coerce").ge(p.min_projected_net_bps)
            & frame[f"mreg_allow_{side}"].eq(1)
        )
        pd.testing.assert_series_equal(mask, expected)
        assert mask.any(), "Fixture must exercise admissions, not only rejections"
    pd.testing.assert_frame_equal(frame, original)


def test_every_diagnostic_combination_has_stable_order() -> None:
    reasons = (
        "htf_context_missing", "structure_parent_missing", "gap_parent",
        "market_regime_not_ready", "regime_flat", "family_mismatch",
        "structure_not_ready", "data_quality_not_ok",
        "one_hour_structure_unresolved", "reclaim_not_meaningful",
        "closed_15m_reclaim_missing", "side_alignment_missing",
    )
    p = HtfRegimeContinuation15mV2().params
    for checks in product((False, True), repeat=len(reasons)):
        row = pd.Series({
            "bos15_parent_identity_ok": checks[1],
            "mreg_ready": checks[3],
            "mreg_state": "continuation" if checks[4] else "flat",
            "mreg_allow_long": checks[5],
            "bos15_structure_ready": checks[6],
            "bos15_quality_ok": checks[7],
            "bos15_structure_trend": "up" if checks[8] else "unknown",
            "hsc_volume_ratio": p.volume_mult if checks[9] else 0,
            "hsc_body_bps": p.min_reclaim_body_bps,
            "hsc_pullback_long": checks[10],
            "rt_allow_long": checks[11],
        })
        result = assess_continuation(
            row, p, context_missing=not checks[0], parent_ineligible=not checks[2],
        )
        expected = tuple(reason for ok, reason in zip(checks, reasons) if not ok)
        assert result.failed_gates == expected
        assert result.primary_failed_gate == (expected[0] if expected else None)
        assert result.eligible == (checks[11] and checks[0])


@pytest.mark.parametrize("value", [np.nan, "0", 0])
def test_missing_or_false_setup_is_not_eligible(value: object) -> None:
    result = assess_continuation(
        pd.Series({"rt_allow_long": value}), HtfRegimeContinuation15mV2().params,
        context_missing=False, parent_ineligible=False,
    )
    assert not result.eligible
    assert "side_alignment_missing" in result.failed_gates


def test_legacy_diagnostic_coercion_is_not_an_admission_rule() -> None:
    # V2 historically treats string 'nan' as truthy after float conversion.
    # Actual prepared setup flags are floats. Characterize, don't silently
    # change the old diagnostic contract during extraction.
    result = assess_continuation(
        pd.Series({"rt_allow_long": "nan"}), HtfRegimeContinuation15mV2().params,
        context_missing=False, parent_ineligible=False,
    )
    assert result.eligible
    assert result.failed_gates


def test_adapter_uses_rules_without_a_second_telescope() -> None:
    strategy = HtfRegimeContinuation15mV2()
    p = strategy.params
    frame = pd.DataFrame([{
        "bos15_structure_ready": True, "bos15_quality_ok": 1,
        "bos15_structure_trend": "down", "hsc_volume_ratio": p.volume_mult,
        "hsc_body_bps": p.min_reclaim_body_bps,
        "hsc_pullback_long": 0, "hsc_pullback_short": 1,
        "hsc_projected_net_long_bps": 0,
        "hsc_projected_net_short_bps": p.min_projected_net_bps,
        "mreg_allow_long": 0, "mreg_allow_short": 1,
        "bos15_htf_aligned_short": 0, "bos15_avwap_relation": "strong_long",
    }])
    out = strategy._apply_price_only_contract(frame)
    assert out.loc[0, "rt_arm_ready"] == 1.
    assert out.loc[0, "rt_allow_short"] == 1.
    assert out.loc[0, "rt_allow_long"] == 0.
    assert out.loc[0, "mreg_avwap_source"] == "unavailable"
