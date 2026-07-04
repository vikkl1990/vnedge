"""Governed paper trial — manifest validation, live funding growth, wiring."""

import json
from pathlib import Path

import pytest

from vnedge.data.schemas import normalize_candles, normalize_funding
from vnedge.runtime.paper_trial import (
    LiveFundingMR,
    TrialManifest,
    append_trial_report,
    build_trial_session,
)

BASE = 1_750_000_000_000
HOUR = 3_600_000

MANIFEST = Path("research/paper_trials/funding_mr_btc_v1_20260703.yaml")


def manifest_dict(**overrides) -> dict:
    base = {
        "trial_id": "t1", "strategy": "funding_mean_reversion_v1",
        "symbol": "BTC/USDT:USDT", "timeframe": "1h", "mode": "live_data_paper",
        "approved_by": "human", "strategy_params": {"extreme_pct": 0.85},
        "starting_equity": 500, "daily_loss_limit_usd": 10,
        "live_orders_enabled": False, "promotion_source_commit": "3b56d20",
    }
    base.update(overrides)
    return base


def write_manifest(tmp_path, **overrides) -> Path:
    import yaml

    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(manifest_dict(**overrides)))
    return path


def test_committed_manifest_loads():
    m = TrialManifest.load(MANIFEST)
    assert m.strategy == "funding_mean_reversion_v1"
    assert m.daily_loss_limit_usd == 10.0
    assert not m.live_orders_enabled
    assert m.strategy_params["extreme_pct"] == 0.85


def test_live_orders_manifest_refused(tmp_path):
    path = write_manifest(tmp_path, live_orders_enabled=True)
    with pytest.raises(ValueError, match="not a paper trial"):
        TrialManifest.load(path)


def test_unapproved_strategy_refused(tmp_path):
    path = write_manifest(tmp_path, strategy="secret_profit_machine")
    with pytest.raises(ValueError, match="no promotion-gate approval"):
        TrialManifest.load(path)


def test_non_human_approval_refused(tmp_path):
    path = write_manifest(tmp_path, approved_by="ai")
    with pytest.raises(ValueError, match="human approval"):
        TrialManifest.load(path)


class FundingFeedStub:
    funding_rate = 0.0042
    quote = (99.99, 100.01)


def test_live_funding_mr_appends_feed_rate():
    seed_funding = normalize_funding(
        [{"timestamp": BASE + i * 8 * HOUR, "fundingRate": 0.0001} for i in range(30)]
    )
    candles = normalize_candles(
        [[BASE + i * HOUR, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(400)]
    )
    strategy = LiveFundingMR(
        seed_funding, FundingFeedStub(),
        funding_pct_window=48, z_window=24,
    )
    rows_before = len(strategy.funding)
    df = strategy.prepare(candles)
    assert len(strategy.funding) == rows_before + 1
    assert strategy.funding["funding_rate"].iloc[-1] == pytest.approx(0.0042)
    # newest bar carries the live rate via backward as-of merge
    assert df["funding_rate"].iloc[-1] == pytest.approx(0.0042)
    # calling again for the same newest bar must not append twice
    strategy.prepare(candles)
    assert len(strategy.funding) == rows_before + 1


def test_trial_session_wiring_and_report(tmp_path):
    import asyncio

    from tests.test_live_paper import FakeFeed  # scripted feed, no network

    manifest = TrialManifest.load(write_manifest(tmp_path))
    history = normalize_candles(
        [[BASE + i * HOUR, 100.0, 100.5, 99.5, 100.0, 5.0] for i in range(400)]
    )
    seed_funding = normalize_funding(
        [{"timestamp": BASE + i * 8 * HOUR, "fundingRate": 0.0001} for i in range(30)]
    )
    feed = FakeFeed([[BASE + 400 * HOUR, 100.0, 100.5, 99.5, 100.0, 5.0]])
    feed.funding_rate = 0.0001

    session = build_trial_session(
        manifest, feed, history, seed_funding, journal_dir=tmp_path
    )
    # the manifest's daily-loss number reached the actual gateway config
    assert session.config.risk.max_daily_loss_usd == 10.0
    assert session.config.starting_equity_usd == 500.0

    report = asyncio.run(session.run(max_bars=1))
    assert report.bars_processed == 1

    reports_path = tmp_path / "t1.reports.jsonl"
    append_trial_report(manifest, report, reports_path)
    record = json.loads(reports_path.read_text().strip())
    assert record["trial_id"] == "t1"
    assert record["promotion_source_commit"] == "3b56d20"
    assert record["report"]["mode"] == "paper_live"
