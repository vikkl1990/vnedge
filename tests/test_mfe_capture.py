"""MFE capture flags exits the bar data cannot justify."""

from __future__ import annotations

from dataclasses import dataclass

from research.mfe_capture import IMPLAUSIBLE_CAPTURE, capture_ratios, score


@dataclass
class _T:
    gross_bps: float
    mfe_bps: float


def test_an_exit_banking_almost_the_whole_excursion_is_flagged() -> None:
    """The public catalogue measured 93.9% median; that is the shape to catch."""
    report = score([_T(94.0, 100.0) for _ in range(50)])
    assert report.verdict == "implausible"
    assert report.implausible is True
    assert report.median > IMPLAUSIBLE_CAPTURE


def test_a_normal_target_exit_reads_as_typical() -> None:
    report = score([_T(40.0, 100.0) for _ in range(50)])
    assert report.verdict == "typical"
    assert report.implausible is False


def test_giving_most_of_the_move_back_is_reported_not_praised() -> None:
    """A low ratio is the honest cost of a rule-based exit, not a defect."""
    assert score([_T(10.0, 100.0)] * 30).verdict == "gives_it_back"


def test_trades_that_never_moved_in_favour_are_skipped_not_zeroed() -> None:
    """Dividing by a zero best-moment says nothing about the exit."""
    ratios = capture_ratios([_T(-5.0, 0.0), _T(50.0, 100.0)])
    assert ratios == [0.5]
    report = score([_T(-5.0, 0.0), _T(50.0, 100.0)])
    assert report.n == 2 and report.scored == 1


def test_losers_pull_the_ratio_negative_and_that_is_kept() -> None:
    """A trade that ran up then lost has a negative capture; it is real signal."""
    ratios = capture_ratios([_T(-20.0, 40.0)])
    assert ratios == [-0.5]


def test_an_empty_book_reports_no_excursions_rather_than_crashing() -> None:
    report = score([])
    assert report.verdict == "no_excursions" and report.scored == 0
