"""The catalogue must lead with evidence, and never flatter a scanner."""

from __future__ import annotations

from vnedge.research.scanner_catalog import (
    EVIDENCE_AUTHORITY,
    EVIDENCE_ORDER,
    build_catalog,
    catalog_payload,
)


def _burn(sid, verdict, kind="judgment", start="2024-01-01", end="2024-12-31"):
    return {"strategy_id": sid, "verdict": verdict, "kind": kind,
            "window_start": start, "window_end": end, "note": "n"}


def _catalog(**over):
    args = dict(strategies=["a_v1", "b_v1"], capital_approved=[], research_only=[],
                shadow_observe=[], killed=[], burn_records=[])
    args.update(over)
    return build_catalog(**args)


def test_a_sealed_failure_displaces_untested() -> None:
    """The bug this pins: a FAIL is stronger evidence than no evidence.

    Ranking by display order made 'untested' outrank 'sealed_fail', so scanners
    with recorded failures read as untried.
    """
    entries = {e.strategy_id: e for e in _catalog(burn_records=[_burn("a_v1", "FAIL")])}
    assert entries["a_v1"].evidence == "sealed_fail"
    assert entries["b_v1"].evidence == "untested"
    assert EVIDENCE_AUTHORITY["sealed_fail"] > EVIDENCE_AUTHORITY["untested"]


def test_display_order_and_authority_are_different_things() -> None:
    """Untested sits mid-table for display and bottom for authority."""
    assert EVIDENCE_ORDER.index("untested") > EVIDENCE_ORDER.index("sealed_fail")
    assert EVIDENCE_AUTHORITY["untested"] < EVIDENCE_AUTHORITY["sealed_fail"]


def test_a_killed_scanner_stays_killed() -> None:
    entries = {e.strategy_id: e for e in _catalog(
        killed=["a_v1"], burn_records=[_burn("a_v1", "PASS")])}
    assert entries["a_v1"].evidence == "killed"
    assert entries["a_v1"].killed is True


def test_at_equal_authority_the_unfavourable_reading_wins() -> None:
    """Two sealed runs disagreeing is not licence to quote the better one."""
    entries = {e.strategy_id: e for e in _catalog(
        burn_records=[_burn("a_v1", "PASS"), _burn("a_v1", "FAIL")])}
    assert entries["a_v1"].evidence == "sealed_fail"
    assert entries["a_v1"].judgments == 2


def test_a_stronger_stage_beats_a_weaker_one() -> None:
    entries = {e.strategy_id: e for e in _catalog(
        burn_records=[_burn("a_v1", "EXPLORATORY_NEGATIVE"), _burn("a_v1", "PASS")])}
    assert entries["a_v1"].evidence == "sealed_pass"


def test_scanners_known_only_to_the_ledger_still_appear() -> None:
    """A judged strategy absent from the registry must not vanish."""
    ids = {e.strategy_id for e in _catalog(burn_records=[_burn("ghost_v1", "FAIL")])}
    assert "ghost_v1" in ids


def test_burned_windows_are_carried_so_nobody_reuses_them() -> None:
    entries = {e.strategy_id: e for e in _catalog(
        burn_records=[_burn("a_v1", "FAIL", start="2021-07-01", end="2023-05-31")])}
    window = entries["a_v1"].burned_windows[0]
    assert (window.start, window.end, window.verdict) == (
        "2021-07-01", "2023-05-31", "FAIL")


def test_payload_reports_capital_count_and_evidence_mix() -> None:
    payload = catalog_payload(_catalog(burn_records=[_burn("a_v1", "FAIL")]))
    assert payload["count"] == 2
    assert payload["capital_approved"] == 0
    assert payload["by_evidence"]["sealed_fail"] == 1
    assert payload["burned_windows"] == 1


def test_the_live_catalogue_reflects_this_checkout() -> None:
    from vnedge.research.scanner_catalog import live_catalog

    payload = live_catalog()
    ids = {s["strategy_id"]: s for s in payload["scanners"]}
    # three families were sealed or selection-failed today; none may read as untested
    for sid in ("structure_bounce_prod_v1", "htf_ma_pullback_4h_v1"):
        assert ids[sid]["evidence"] == "sealed_fail", sid
    assert ids["breakout_continuity_v1"]["evidence"] == "selection_fail"
    assert payload["capital_approved"] == 0


def test_the_endpoint_serves_the_catalogue_behind_auth() -> None:
    from starlette.testclient import TestClient

    from vnedge.dashboard.app import SnapshotProvider, create_app

    client = TestClient(create_app(SnapshotProvider(), token="t"))
    assert client.get("/api/scanners").status_code in (401, 403)
    body = client.get("/api/scanners?token=t").json()
    assert body["capital_approved"] == 0
    assert any(s["evidence"] == "sealed_fail" for s in body["scanners"])
