"""UI honesty + tooling changes: fleet aggregate, active-lane journal filter,
per-exchange cost model, and the observation-only paper TP ladder join."""

from __future__ import annotations

import json

from vnedge.dashboard.app import _cost_model_payload
from vnedge.dashboard.trade_journal import build_trade_journal
from vnedge.runtime.multi_lane import MultiLaneProvider, _fleet_aggregate


def _write(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_fleet_aggregate_splits_paper_shadow_and_excludes_errors():
    lanes = [
        {"lane_id": "a", "mode": "paper (live data)", "equity": 510.0,
         "realized_pnl": 10.0, "unrealized_pnl": 0.0, "fees_usd": 1.0, "risk_status": "ok"},
        {"lane_id": "b", "mode": "paper (live data)", "equity": 483.0,
         "realized_pnl": -17.0, "unrealized_pnl": 0.0, "fees_usd": 2.0, "risk_status": "ok"},
        {"lane_id": "c", "mode": "shadow (live data)", "equity": 500.0,
         "realized_pnl": 0.0, "unrealized_pnl": 0.0, "fees_usd": 0.0, "risk_status": "ok",
         "shadow_perf": {"net_usd": -131.0, "virtual_trades": 103}},
        {"lane_id": "err", "mode": "shadow (live data)", "equity": 0.0,
         "realized_pnl": 0.0, "unrealized_pnl": 0.0, "risk_status": "lane_error"},
    ]
    f = _fleet_aggregate(lanes)
    assert f["lanes"] == 3  # error lane excluded — a crash is not a $0 account
    assert f["equity"] == 1493.0
    assert f["starting_equity"] == 1500.0
    assert f["realized_pnl"] == -7.0
    assert f["paper_lanes"] == 2 and f["shadow_lanes"] == 1
    assert f["shadow_virtual_net_usd"] == -131.0 and f["shadow_virtual_trades"] == 103
    assert f["profitable_lanes"] == 1 and f["losing_lanes"] == 1
    assert f["return_pct"] < 0  # honest: the fleet is net negative


def test_fleet_aggregate_attached_to_multilane_snapshot():
    provider = MultiLaneProvider("a")
    base = {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "fills": 0, "fees_usd": 0.0,
            "risk_status": "ok", "feed_health": {"candles": "ok"}, "positions": [],
            "open_orders": [], "session": {}, "mode": "paper"}
    provider._publish("a", "binanceusdm", {**base, "equity": 510.0, "realized_pnl": 10.0})
    provider._publish("b", "bybit", {**base, "equity": 490.0, "realized_pnl": -10.0})
    snap = provider.latest()
    assert "fleet" in snap
    assert snap["fleet"]["equity"] == 1000.0
    assert snap["fleet"]["lanes"] == 2
    # the flat equity is still the primary lane only — fleet is the honest total
    assert snap["equity"] == 510.0


def test_journal_fleet_view_excludes_retired_lanes(tmp_path):
    # active lane: +10 realized
    _write(tmp_path / "live.fills.jsonl", [
        {"ts": "2026-07-27T00:00:00Z", "lane": "live", "side": "buy", "price": 100,
         "quantity": 1, "realized_pnl_usd": 0, "fee_usd": 0.05, "symbol": "BTC/USDT"},
        {"ts": "2026-07-27T01:00:00Z", "lane": "live", "side": "sell", "price": 110,
         "quantity": 1, "realized_pnl_usd": 10, "fee_usd": 0.05, "symbol": "BTC/USDT",
         "client_order_id": "exit-live-1"},
    ])
    # retired lane still on disk: -50 realized — must NOT pollute the fleet view
    _write(tmp_path / "retired.fills.jsonl", [
        {"ts": "2026-01-01T00:00:00Z", "lane": "retired", "side": "buy", "price": 100,
         "quantity": 1, "realized_pnl_usd": 0, "fee_usd": 0.05, "symbol": "ETH/USDT"},
        {"ts": "2026-01-01T01:00:00Z", "lane": "retired", "side": "sell", "price": 50,
         "quantity": 1, "realized_pnl_usd": -50, "fee_usd": 0.05, "symbol": "ETH/USDT"},
    ])
    snap = {"lane_id": "live", "lanes": [{"lane_id": "live"}]}
    payload = build_trade_journal(snapshot=snap, journal_dir=tmp_path, lane="", limit=200)
    assert payload["summary"]["fill_ledgers_scanned"] == 1
    assert payload["summary"]["active_lanes"] == 1
    assert payload["summary"]["actual_realized_pnl_usd"] == 10.0

    # no snapshot -> can't tell what's active -> fall back to scanning all
    fallback = build_trade_journal(snapshot=None, journal_dir=tmp_path, lane="", limit=200)
    assert fallback["summary"]["fill_ledgers_scanned"] == 2


def test_paper_tp_ladder_joins_by_client_order_id(tmp_path):
    _write(tmp_path / "pt.fills.jsonl", [
        {"ts": "2026-07-27T00:00:00Z", "lane": "pt", "side": "buy", "price": 100,
         "quantity": 1, "realized_pnl_usd": 0, "fee_usd": 0.05, "symbol": "ETH/USD"},
        {"ts": "2026-07-27T02:00:00Z", "lane": "pt", "side": "sell", "price": 106,
         "quantity": 1, "realized_pnl_usd": 6, "fee_usd": 0.05, "symbol": "ETH/USD",
         "client_order_id": "exit-abc"},
    ])
    _write(tmp_path / "pt.journal.jsonl", [
        {"ts": "2026-07-27T02:00:00Z", "kind": "live_paper_exit", "payload": {
            "reason": "take_profit", "state": "filled", "client_order_id": "exit-abc",
            "take_profit_levels": [102, 104, 108], "tp_reached": 2, "mfe_price": 106}},
    ])
    snap = {"lane_id": "pt", "lanes": [{"lane_id": "pt"}]}
    payload = build_trade_journal(snapshot=snap, journal_dir=tmp_path, lane="", limit=200)
    paper = [t for t in payload["closed_trades"] if t.get("kind") == "actual_closing_fill"]
    assert len(paper) == 1
    trade = paper[0]
    assert trade["take_profit_levels"] == [102, 104, 108]
    assert trade["tp_reached"] == 2
    assert trade["resolution"] == "take_profit"
    # captured bps still computed from the real entry->exit prices (gross)
    assert trade["captured_bps_basis"] == "gross"


def test_cost_model_exposes_all_venues():
    cm = _cost_model_payload()
    by = {e["exchange"]: e for e in cm["exchanges"]}
    assert {"binanceusdm", "bybit", "delta_india"} <= set(by)
    # every venue carries a human label and a round-trip cost the calculator uses
    for prof in by.values():
        assert prof["label"]
        assert prof["taker_round_trip_cost_bps"] > 0
    assert by["bybit"]["taker_bps"] == 5.5  # bybit's taker differs from binance
