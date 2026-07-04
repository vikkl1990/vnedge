"""Continuous research loop — record shape, publishing, dashboard endpoint."""

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from vnedge.backtest.metrics import BacktestMetrics
from vnedge.backtest.walk_forward import (
    SPARSE_STRATEGY_GATES,
    WalkForwardResult,
    WindowResult,
)
from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.research import continuous_research as cr

BASE = 1_750_000_000_000


def ts(i):
    return pd.Timestamp(BASE + i * 3_600_000, unit="ms", tz="UTC")


def metrics(num_trades=6, net=15.0, win_rate=60.0):
    return BacktestMetrics(
        num_trades=num_trades, skipped_by_sizing=0, net_profit_usd=net,
        return_pct=net / 5, max_drawdown_pct=2.0, sharpe=1.0, sortino=1.1,
        profit_factor=1.5, win_rate_pct=win_rate, avg_win_usd=6.0,
        avg_loss_usd=-4.0, total_fees_usd=1.0, total_funding_usd=0.0,
        exit_reasons={},
    )


def make_result(n=5):
    return WalkForwardResult(windows=tuple(
        WindowResult(i, ts(i * 100), ts(i * 100 + 60), ts(i * 100 + 90),
                     {"p": 1}, metrics(), metrics())
        for i in range(n)
    ))


def test_wf_record_pass():
    record = cr.wf_record("funding_mean_reversion_v1", "BTC/USDT:USDT",
                          make_result(), SPARSE_STRATEGY_GATES)
    assert record["verdict"] == "PASS"
    assert record["oos_trades"] == 30
    assert record["windows"] == 5
    assert record["reasons"] == []
    for field in ("strategy", "symbol", "oos_net_usd",
                  "profitable_windows_pct", "traded_windows", "updated"):
        assert field in record


def test_wf_record_reject_carries_reasons():
    record = cr.wf_record("x", "BTC/USDT:USDT", make_result(2), SPARSE_STRATEGY_GATES)
    assert record["verdict"] == "REJECT"
    assert any("splits" in r for r in record["reasons"])


def test_publish_atomic_and_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "OUT_DIR", tmp_path / "live_research")
    records = [cr.wf_record("s", "BTC/USDT:USDT", make_result(), SPARSE_STRATEGY_GATES)]
    cr.publish(records, started=0.0)
    cr.publish(records, started=0.0)  # second cycle appends feed, replaces latest
    latest = json.loads((tmp_path / "live_research" / "latest.json").read_text())
    assert latest["results"][0]["verdict"] == "PASS"
    assert "not a promotion" in latest["note"]
    feed = (tmp_path / "live_research" / "feed.jsonl").read_text().strip().splitlines()
    assert len(feed) == 2


def test_research_endpoint(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "x"})
    research = tmp_path / "latest.json"
    research.write_text(json.dumps({"generated_at": "t", "results": [{"verdict": "PASS"}]}))
    client = TestClient(create_app(provider, token="t3st", research_path=research))
    assert client.get("/research").status_code == 401
    payload = client.get("/research?token=t3st").json()
    assert payload["results"][0]["verdict"] == "PASS"


def test_research_endpoint_missing_file(tmp_path):
    provider = SnapshotProvider()
    provider.publish({"mode": "x"})
    client = TestClient(create_app(provider, token="t3st",
                                   research_path=tmp_path / "nope.json"))
    assert client.get("/research?token=t3st").json() == {"results": []}
