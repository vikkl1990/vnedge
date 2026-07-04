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


def make_record(verdict="PASS", net=20.0, trades=6):
    return {"strategy": "funding_mean_reversion_v1", "symbol": "BTC/USDT:USDT",
            "verdict": verdict, "oos_net_usd": net, "oos_trades": trades,
            "reasons": [] if verdict == "PASS" else ["aggregate OOS net not positive"]}


def test_attribution_by_side():
    from vnedge.backtest.backtester import Trade

    def trade(side, net):
        return Trade(side=side, quantity=1.0, entry_ts=ts(0), entry_price=100.0,
                     exit_ts=ts(1), exit_price=100.0 + net, exit_reason="stop",
                     gross_pnl_usd=net, fees_usd=0.0, funding_usd=0.0,
                     entry_reason="t")

    windows = tuple(
        WindowResult(i, ts(0), ts(1), ts(2), {}, metrics(), metrics(),
                     test_trades=(trade("long", 10.0), trade("short", -4.0)))
        for i in range(3)
    )
    att = cr.side_attribution(WalkForwardResult(windows=windows))
    assert att["long"] == {"trades": 3, "net_usd": 30.0, "win_rate_pct": 100.0}
    assert att["short"]["trades"] == 3 and att["short"]["net_usd"] == -12.0


def test_drift_verdict_flip_fires_once():
    prev = [make_record("PASS"), make_record("PASS")]
    alerts = cr.compute_drift_alerts(prev, make_record("REJECT", net=-5.0))
    assert any(a["rule_id"] == "drift_verdict_flip" for a in alerts)
    assert any(a["rule_id"] == "drift_oos_sign_flip" for a in alerts)
    # next cycle: REJECT again — edge conditions must NOT refire
    prev2 = prev + [make_record("REJECT", net=-5.0)]
    again = cr.compute_drift_alerts(prev2, make_record("REJECT", net=-6.0))
    assert not any(a["rule_id"] == "drift_verdict_flip" for a in again)
    assert not any(a["rule_id"] == "drift_oos_sign_flip" for a in again)


def test_drift_consecutive_rejects_fires_exactly_at_threshold():
    prev = [make_record("PASS")] + [make_record("REJECT")] * 2
    alerts = cr.compute_drift_alerts(prev, make_record("REJECT"))
    assert any(a["rule_id"] == "drift_consecutive_rejects" and
               a["severity"] == "critical" for a in alerts)
    prev4 = prev + [make_record("REJECT")]
    again = cr.compute_drift_alerts(prev4, make_record("REJECT"))
    assert not any(a["rule_id"] == "drift_consecutive_rejects" for a in again)


def test_drift_trade_collapse_edge_triggered():
    prev = [make_record(trades=20) for _ in range(8)]
    alerts = cr.compute_drift_alerts(prev, make_record(trades=4))
    assert any(a["rule_id"] == "drift_trade_collapse" for a in alerts)
    prev2 = prev + [make_record(trades=4)]
    again = cr.compute_drift_alerts(prev2, make_record(trades=3))
    assert not any(a["rule_id"] == "drift_trade_collapse" for a in again)


def test_quiet_when_healthy():
    prev = [make_record() for _ in range(10)]
    assert cr.compute_drift_alerts(prev, make_record()) == []


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
