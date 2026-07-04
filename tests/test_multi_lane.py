"""Multi-lane shadow — provider fan-in, primary flat snapshot, comparison array."""

from vnedge.runtime import multi_lane
from vnedge.runtime.multi_lane import LaneSpec, MultiLaneProvider, MultiLaneShadowRunner
from vnedge.runtime.multi_lane_shadow import build_lane_specs_from_env
from vnedge.runtime.runner_config import RunnerMode


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
    assert spec.mode is RunnerMode.SHADOW


def test_publish_error_adds_faulted_lane():
    p = MultiLaneProvider("binance")
    p.publish_error("bybit", "bybit", "BTC/USDT:USDT", "build failed")
    out = p.latest()
    assert out["risk_status"] == "lane_error"
    assert out["lanes"][0]["feed"] == "error"


def test_lane_specs_expand_from_env():
    specs = build_lane_specs_from_env({
        "MULTI_LANE_EXCHANGES": "binanceusdm,bybit",
        "MULTI_LANE_SYMBOLS": "BTC/USDT:USDT,ETH/USDT:USDT",
        "MULTI_LANE_PRIMARY_EXCHANGE": "bybit",
        "MULTI_LANE_PRIMARY_SYMBOL": "ETH/USDT:USDT",
    })
    assert len(specs) == 4
    primary = [spec for spec in specs if spec.is_primary]
    assert len(primary) == 1
    assert primary[0].exchange == "bybit"
    assert primary[0].symbol == "ETH/USDT:USDT"
    assert all(spec.mode is RunnerMode.SHADOW for spec in specs)


async def test_runner_continues_when_one_lane_build_fails(monkeypatch, tmp_path):
    events = []

    class FakeFeed:
        def __init__(self, lane_id):
            self.lane_id = lane_id

        async def start(self):
            events.append(("start", self.lane_id))

        async def stop(self):
            events.append(("stop", self.lane_id))

    class FakeSession:
        def __init__(self, lane_id):
            self.lane_id = lane_id

        async def run(self, *, deadline_seconds=None):
            events.append(("run", self.lane_id, deadline_seconds))

    async def fake_build_lane(spec, provider, journal_dir):
        if spec.exchange == "bad":
            raise RuntimeError("boom")
        return multi_lane._LaneRuntime(
            spec=spec,
            session=FakeSession(spec.lane_id),
            feed=FakeFeed(spec.lane_id),
        )

    monkeypatch.setattr(multi_lane, "build_lane", fake_build_lane)
    provider = MultiLaneProvider("good")
    runner = MultiLaneShadowRunner(
        [
            LaneSpec("bad", "bad", "BTC/USDT:USDT"),
            LaneSpec("good", "bybit", "BTC/USDT:USDT"),
        ],
        tmp_path,
        provider,
    )

    await runner.run(deadline_seconds=0.01)

    assert ("start", "good") in events
    assert ("run", "good", 0.01) in events
    assert ("stop", "good") in events
    latest = provider.latest()
    assert latest is not None
    faulted = [lane for lane in latest["lanes"] if lane["lane_id"] == "bad"]
    assert faulted and faulted[0]["risk_status"] == "lane_error"
