"""Tests for the trial ledger — the honest n_trials source for DSR."""

from __future__ import annotations

import pytest

from vnedge.research.experiment_recorder import ExperimentRecorder
from vnedge.research.trial_ledger import TrialLedger, window_key


@pytest.fixture()
def ledger(tmp_path):
    return TrialLedger(recorder=ExperimentRecorder(tmp_path / "experiments"))


def test_window_key_is_stable_and_order_insensitive() -> None:
    a = window_key(start_ms=0, end_ms=86_400_000, symbols=["BTCUSDT", "ETHUSDT"])
    b = window_key(start_ms=0, end_ms=86_400_000, symbols=["ETHUSDT", "BTCUSDT"])
    assert a == b, "symbol order must not change the window identity"
    c = window_key(start_ms=0, end_ms=2 * 86_400_000, symbols=["BTCUSDT", "ETHUSDT"])
    assert a != c, "a different span is a different question"


def test_trial_count_grows_with_every_scored_config(ledger) -> None:
    w = window_key(start_ms=0, end_ms=90 * 86_400_000, symbols=["BTCUSDT"])
    assert ledger.trial_count(w) == 0
    for chase in (10.0, 20.0, 30.0):
        ledger.record(arm="coil", window=w, params={"max_chase_bps": chase},
                      metrics={"pf": 1.2, "net_bps": 100.0}, symbols=["BTCUSDT"],
                      sharpe=0.1)
    assert ledger.trial_count(w) == 3


def test_discarded_variants_still_count(ledger) -> None:
    """The whole point: a losing sweep is a trial and must be charged for."""
    w = window_key(start_ms=0, end_ms=90 * 86_400_000, symbols=["BTCUSDT"])
    ledger.record(arm="coil", window=w, params={"be_r": 1.0}, metrics={"pf": 1.3},
                  symbols=["BTCUSDT"], sharpe=0.12)
    ledger.record(arm="coil", window=w, params={"be_r": 2.0}, metrics={"pf": 0.94},
                  symbols=["BTCUSDT"], sharpe=-0.03, note="rejected")
    assert ledger.trial_count(w) == 2
    assert sorted(ledger.trial_sharpes(w)) == [-0.03, 0.12]


def test_trials_are_scoped_to_their_window(ledger) -> None:
    w1 = window_key(start_ms=0, end_ms=30 * 86_400_000, symbols=["BTCUSDT"])
    w2 = window_key(start_ms=0, end_ms=90 * 86_400_000, symbols=["BTCUSDT"])
    ledger.record(arm="coil", window=w1, params={}, metrics={}, symbols=["BTCUSDT"], sharpe=0.2)
    ledger.record(arm="coil", window=w2, params={}, metrics={}, symbols=["BTCUSDT"], sharpe=0.3)
    assert ledger.trial_count(w1) == 1
    assert ledger.trial_count(w2) == 1


def test_arm_scoped_counts_and_summary(ledger) -> None:
    w = window_key(start_ms=0, end_ms=90 * 86_400_000, symbols=["BTCUSDT"])
    ledger.record(arm="coil", window=w, params={}, metrics={}, symbols=["BTCUSDT"], sharpe=0.1)
    ledger.record(arm="ignition", window=w, params={}, metrics={}, symbols=["BTCUSDT"], sharpe=0.2)
    ledger.record(arm="ignition", window=w, params={}, metrics={}, symbols=["BTCUSDT"], sharpe=0.3)
    assert ledger.trial_count(w, arm="ignition") == 2
    report = ledger.summary(w)
    assert report["trials"] == 3
    assert report["by_arm"] == {"coil": 1, "ignition": 2}
    assert len(report["sharpes"]) == 3
    assert report["git_shas"], "every run records the code version that produced it"


def test_dsr_consumes_the_ledger_end_to_end(ledger) -> None:
    """The loop that matters: recorded trials feed the deflated Sharpe."""
    import numpy as np

    from vnedge.ml.validation import deflated_sharpe_ratio

    w = window_key(start_ms=0, end_ms=90 * 86_400_000, symbols=["BTCUSDT"])
    rng = np.random.default_rng(3)
    for k in range(12):
        ledger.record(arm="coil", window=w, params={"variant": k}, metrics={},
                      symbols=["BTCUSDT"], sharpe=float(rng.normal(0.05, 0.06)))
    daily = rng.normal(2.0, 30.0, 90)

    honest = deflated_sharpe_ratio(
        daily, n_trials=ledger.trial_count(w), trial_sharpes=ledger.trial_sharpes(w)
    )
    understated = deflated_sharpe_ratio(
        daily, n_trials=2, trial_sharpes=ledger.trial_sharpes(w)
    )
    # charging for the real search can only lower the deflated Sharpe
    assert honest <= understated
