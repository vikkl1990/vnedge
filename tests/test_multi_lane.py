"""Multi-lane shadow — provider fan-in, primary flat snapshot, comparison array."""

from vnedge.runtime.multi_lane import LaneSpec, MultiLaneProvider


def snap(equity, fills=0, realized=0.0, symbol="BTC/USDT:USDT"):
    return {
        "mode": "paper (live data)", "symbol": symbol, "equity": equity,
        "realized_pnl": realized, "unrealized_pnl": 0.0, "fills": fills,
        "fees_usd": 0.5 * fills, "risk_status": "ok",
        "feed_health": {"candles": "ok"}, "positions": [],
    }


def test_empty_provider_returns_none():
    assert MultiLaneProvider("a").latest() is None


def test_primary_lane_is_flat_top_level():
    p = MultiLaneProvider("binance")
    p.sink("bybit", "bybit").publish(snap(510.0))
    p.sink("binance", "binanceusdm").publish(snap(505.0))
    out = p.latest()
    # top-level flat snapshot = the PRIMARY (binance) lane, not the first published
    assert out["equity"] == 505.0
    assert out["lane_id"] == "binance"


def test_lanes_comparison_array():
    p = MultiLaneProvider("binance")
    p.sink("binance", "binanceusdm").publish(snap(505.0, fills=2, realized=5.0))
    p.sink("bybit", "bybit").publish(snap(498.0, fills=3, realized=-2.0))
    out = p.latest()
    lanes = out["lanes"]
    assert len(lanes) == 2
    by_ex = {lane["exchange"]: lane for lane in lanes}
    assert by_ex["binanceusdm"]["equity"] == 505.0 and by_ex["binanceusdm"]["fills"] == 2
    assert by_ex["bybit"]["realized_pnl"] == -2.0
    for lane in lanes:
        for f in ("lane_id", "exchange", "symbol", "equity", "realized_pnl",
                  "fills", "fees_usd", "risk_status", "feed"):
            assert f in lane


def test_lane_order_is_publish_order():
    p = MultiLaneProvider("binance")
    p.sink("bybit", "bybit").publish(snap(1.0))
    p.sink("binance", "binanceusdm").publish(snap(2.0))
    assert [lane["exchange"] for lane in p.latest()["lanes"]] == ["bybit", "binanceusdm"]


def test_updates_replace_not_append():
    p = MultiLaneProvider("binance")
    sink = p.sink("binance", "binanceusdm")
    sink.publish(snap(500.0))
    sink.publish(snap(507.0))  # same lane updates
    assert len(p.latest()["lanes"]) == 1
    assert p.latest()["equity"] == 507.0


def test_falls_back_to_first_lane_when_primary_absent():
    p = MultiLaneProvider("nonexistent_primary")
    p.sink("bybit", "bybit").publish(snap(499.0))
    out = p.latest()
    assert out["lane_id"] == "bybit"  # primary missing -> first published lane


def test_lane_spec_defaults():
    spec = LaneSpec(lane_id="x", exchange="bybit", symbol="BTC/USDT:USDT")
    assert spec.starting_equity == 500.0
    assert spec.daily_loss_usd == 10.0
    assert spec.is_primary is False
