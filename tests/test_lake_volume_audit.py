"""The audit must name the failure mode, not just flag 'not 1.0'."""

from __future__ import annotations

from research.lake_volume_audit import DUPLICATE_FLOOR, HEALTHY_FLOOR, classify


def test_classifies_the_two_failure_modes_apart() -> None:
    """Under-recording and double-recording need opposite responses."""
    assert classify(1.00) == "ok"
    assert classify(0.995) == "ok"          # normal websocket drop
    assert classify(0.606) == "COVERAGE LOSS"  # the 20 Aug 02:00 crash-loop hour
    assert classify(1.99) == "DUPLICATED"   # two recorders on one partition
    assert classify(2.00) == "DUPLICATED"


def test_thresholds_leave_no_unclassified_gap() -> None:
    assert HEALTHY_FLOOR < DUPLICATE_FLOOR
    assert classify(HEALTHY_FLOOR) == "ok"
    assert classify(HEALTHY_FLOOR - 0.001) == "COVERAGE LOSS"
    assert classify(DUPLICATE_FLOOR) == "DUPLICATED"
