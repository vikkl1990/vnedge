"""Tests for the scanner scorecard / auto-suppression logic."""
from vnedge.research.scanner_scorecard import (
    ScannerScorecard, classify,
    STATUS_ACTIVE, STATUS_REDUCED, STATUS_SUPPRESSED, STATUS_LEARNING,
    MIN_SAMPLES,
)


def test_thin_evidence_is_learning_not_trusted():
    s = classify(0.5, MIN_SAMPLES - 1)
    assert s.status == STATUS_LEARNING and s.weight == 0.0


def test_negative_expectancy_suppressed():
    s = classify(-0.10, 50)
    assert s.status == STATUS_SUPPRESSED and s.weight == 0.0


def test_thin_positive_is_reduced():
    s = classify(0.0, 50)  # between suppress(-0.05) and reduce(0.02)
    assert s.status == STATUS_REDUCED and s.weight == 0.5


def test_clear_positive_is_active():
    s = classify(0.20, 50)
    assert s.status == STATUS_ACTIVE and s.weight == 1.0


def test_noise_cannot_unsuppress():
    # a previously-suppressed scanner with a tiny positive sample stays suppressed
    s = classify(0.20, 10, was_suppressed=True)   # samples < RECOVERY_MIN_SAMPLES
    assert s.status == STATUS_SUPPRESSED


def test_suppressed_recovers_only_on_strong_sustained_evidence():
    s = classify(0.10, 30, was_suppressed=True)   # >= recovery gates
    assert s.status == STATUS_ACTIVE


def test_scorecard_evaluate_and_suppressed_list(tmp_path):
    store = tmp_path / "scorecard.json"
    card = ScannerScorecard(store)
    stats = card.evaluate({
        "good_v1": (0.15, 40),
        "bad_v1": (-0.08, 40),
        "thin_v1": (0.00, 40),
        "young_v1": (0.9, 3),
    })
    assert stats["good_v1"].status == STATUS_ACTIVE
    assert stats["bad_v1"].status == STATUS_SUPPRESSED
    assert stats["thin_v1"].status == STATUS_REDUCED
    assert stats["young_v1"].status == STATUS_LEARNING
    assert ScannerScorecard.suppressed(stats) == ["bad_v1"]
    assert store.exists()

    # reload persists suppression memory: bad_v1 now needs recovery gates
    card2 = ScannerScorecard(store)
    again = card2.evaluate({"bad_v1": (0.03, 25)})  # positive but below recovery 0.05
    assert again["bad_v1"].status == STATUS_SUPPRESSED
