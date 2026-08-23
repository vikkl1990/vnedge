from vnedge.strategy.regime_router import Regime, RegimeRouter
from vnedge.strategy.scanner_observability import (
    ScannerCandidate,
    SetupLifecycle,
    arbitrate_conflicts,
    enrich_evaluation,
)


def test_evaluation_enrichment_is_read_only_and_reports_near_miss():
    source = {
        "fired": False,
        "eligible": False,
        "all_failed_gates": ["volume_confirmation_failed"],
        "features": {"setup_ready": True, "break_long": False},
        "distance_to_threshold": {"volume_ratio_shortfall": 0.12},
    }
    result = enrich_evaluation(source)
    assert source.get("setup_lifecycle") is None
    assert result["setup_lifecycle"] == "armed"
    assert result["near_miss"]["counterfactual_only"] is True
    assert result["near_miss"]["closest_distance"] == 0.12


def test_conflict_arbiter_refuses_opposed_candidates():
    result = arbitrate_conflicts(
        [
            ScannerCandidate("a", "BTC", "long", 0.9, SetupLifecycle.ACCEPTED),
            ScannerCandidate("b", "BTC", "short", 0.8, SetupLifecycle.COST_APPROVED),
        ]
    )
    assert result["state"] == "conflict"
    assert result["selected"] is None


def test_aligned_arbiter_selects_furthest_lifecycle():
    result = arbitrate_conflicts(
        [
            ScannerCandidate("watch", "BTC", "long", 1.0, SetupLifecycle.ARMED),
            ScannerCandidate("accepted", "BTC", "long", 0.1, SetupLifecycle.ACCEPTED),
        ]
    )
    assert result["state"] == "aligned"
    assert result["selected"] == "accepted"
    assert result["read_only"] is True


def test_regime_router_knows_new_sleeves_but_grants_no_stress_permission():
    router = RegimeRouter()
    router.regime = Regime.EXPAND
    assert router.allows("session_continuation_15m_v1") is True
    assert router.allows("trend_pullback_1h_v1") is True
    router.regime = Regime.STRESS
    assert router.allows("session_continuation_15m_v1") is False
