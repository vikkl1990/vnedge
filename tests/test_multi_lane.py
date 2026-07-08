"""Multi-lane shadow — provider fan-in, primary flat snapshot, comparison array."""

from vnedge.runtime import multi_lane
from vnedge.runtime.multi_lane import LaneSpec, MultiLaneProvider, MultiLaneShadowRunner
from vnedge.runtime.multi_lane_shadow import build_lane_specs_from_env
from vnedge.runtime.runner_config import RunnerMode


def snap(equity, fills=0, realized=0.0, symbol="BTC/USDT:USDT",
         strategy_id="funding_mean_reversion_v1"):
    return {
        "mode": "paper (live data)", "symbol": symbol, "equity": equity,
        "strategy_id": strategy_id,
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
    # dashboard lane matrix labels mode + strategy per lane
    assert by_ex["binanceusdm"]["mode"] == "paper (live data)"
    assert by_ex["binanceusdm"]["strategy_id"] == "funding_mean_reversion_v1"
    for lane in lanes:
        for f in ("lane_id", "exchange", "symbol", "mode", "strategy_id",
                  "equity", "realized_pnl",
                  "fills", "fees_usd", "risk_status", "feed"):
            assert f in lane


def test_lane_summary_carries_feed_and_eval_observability():
    p = MultiLaneProvider("binance")
    s = snap(505.0)
    s["feed_health"] = {
        "candles": "ok",
        "exchange": "binanceusdm (live ws)",
        "last_update_ms": 1234.0,
    }
    s["session"] = {
        "last_eval": {
            "fired": False,
            "features": {"funding_pct": 0.62, "close_z": -0.4},
            "thresholds": {"extreme_pct": 0.85, "z_entry": 1.5},
        },
    }
    p.sink("binance", "binanceusdm").publish(s)
    lane = p.latest()["lanes"][0]
    assert lane["feed_mode"] == "binanceusdm (live ws)"
    assert lane["staleness_ms"] == 1234.0
    assert lane["last_eval"]["features"]["funding_pct"] == 0.62
    assert lane["last_eval"]["thresholds"]["z_entry"] == 1.5


def test_lane_summary_degrades_without_feed_or_eval():
    p = MultiLaneProvider("binance")
    p.sink("binance", "binanceusdm").publish(snap(505.0))
    lane = p.latest()["lanes"][0]
    assert lane["feed_mode"] == ""
    assert lane["staleness_ms"] is None
    assert lane["last_eval"] is None


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
    # single explicit mode: pure exchange x symbol grid expansion
    specs = build_lane_specs_from_env({
        "MULTI_LANE_EXCHANGES": "binanceusdm,bybit",
        "MULTI_LANE_SYMBOLS": "BTC/USDT:USDT,ETH/USDT:USDT",
        "MULTI_LANE_MODES": "shadow",
        "MULTI_LANE_PRIMARY_EXCHANGE": "bybit",
        "MULTI_LANE_PRIMARY_SYMBOL": "ETH/USDT:USDT",
    })
    assert len(specs) == 4
    primary = [spec for spec in specs if spec.is_primary]
    assert len(primary) == 1
    assert primary[0].exchange == "bybit"
    assert primary[0].symbol == "ETH/USDT:USDT"
    assert all(spec.mode is RunnerMode.SHADOW for spec in specs)


def test_lane_specs_default_runs_both_modes_per_venue():
    # default env: Binance/Bybit governed paper+shadow plus Delta shadow.
    specs = build_lane_specs_from_env({})
    assert len(specs) == 5
    assert {s.mode for s in specs} == {RunnerMode.PAPER, RunnerMode.SHADOW}
    ids = {s.lane_id for s in specs}
    # governed paper trials keep their exact ids (continue their account files)
    assert "funding_mr_btc_v1_20260703" in ids
    assert "funding_mr_bybit_20260704" in ids
    # shadow lanes are distinct, isolated ids
    assert "funding_mr_binanceusdm_btc_usdt_usdt_shadow" in ids
    assert "funding_mr_bybit_btc_usdt_usdt_shadow" in ids
    assert "trend_continuation_delta_india_btc_usd_usd_shadow" in ids
    delta = next(s for s in specs if s.exchange == "delta_india")
    assert delta.symbol == "BTC/USD:USD"
    assert delta.mode is RunnerMode.SHADOW
    assert delta.strategy_id == "trend_continuation_v1"
    # the flat top-level snapshot is the governed Binance PAPER lane
    primary = [s for s in specs if s.is_primary]
    assert len(primary) == 1
    assert primary[0].lane_id == "funding_mr_btc_v1_20260703"
    assert primary[0].mode is RunnerMode.PAPER


def test_delta_paper_requires_explicit_opt_in():
    specs = build_lane_specs_from_env({
        "MULTI_LANE_EXCHANGES": "delta_india",
        "MULTI_LANE_SYMBOLS": "BTC/USDT:USDT",
        "MULTI_LANE_MODES": "paper,shadow",
    })
    assert len(specs) == 1
    assert specs[0].exchange == "delta_india"
    assert specs[0].symbol == "BTC/USD:USD"
    assert specs[0].mode is RunnerMode.SHADOW
    assert specs[0].strategy_id == "trend_continuation_v1"


def test_delta_paper_opt_in_still_uses_candle_only_strategy():
    specs = build_lane_specs_from_env({
        "MULTI_LANE_EXCHANGES": "delta_india",
        "MULTI_LANE_SYMBOLS": "BTC/USDT:USDT",
        "MULTI_LANE_MODES": "paper",
        "MULTI_LANE_DELTA_PAPER": "1",
    })
    assert len(specs) == 1
    assert specs[0].symbol == "BTC/USD:USD"
    assert specs[0].mode is RunnerMode.PAPER
    assert specs[0].strategy_id == "trend_continuation_v1"


def test_lane_specs_reject_unknown_mode():
    import pytest
    with pytest.raises(ValueError, match="unknown multi-lane mode"):
        build_lane_specs_from_env({"MULTI_LANE_MODES": "paper,bogus"})


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


def test_build_strategy_selects_trend_continuation():
    import pandas as pd

    from vnedge.runtime.multi_lane import _build_strategy
    from vnedge.strategy.trend_continuation import TrendContinuation

    spec = LaneSpec(lane_id="x", exchange="bybit", symbol="XRP/USDT:USDT",
                    strategy_id="trend_continuation_v1", strategy_params={})
    strat = _build_strategy(
        spec, pd.DataFrame(columns=["timestamp", "funding_rate"]), feed=None)
    assert isinstance(strat, TrendContinuation)
    assert strat.strategy_id == "trend_continuation_v1"


def test_build_strategy_selects_quant_signal_pack():
    import pandas as pd

    from vnedge.runtime.multi_lane import _build_strategy
    from vnedge.strategy.quant_signal_pack import QuantSignalPack

    spec = LaneSpec(lane_id="x", exchange="bybit", symbol="SOL/USDT:USDT",
                    strategy_id="quant_signal_pack_v1", strategy_params={})
    strat = _build_strategy(
        spec, pd.DataFrame(columns=["timestamp", "funding_rate"]), feed=None)
    assert isinstance(strat, QuantSignalPack)
    assert strat.strategy_id == "quant_signal_pack_v1"


def test_build_strategy_selects_signal_arbiter_composite():
    import pandas as pd

    from vnedge.runtime.multi_lane import _build_strategy
    from vnedge.strategy.composite import CompositeSignalStrategy

    spec = LaneSpec(
        lane_id="arb",
        exchange="bybit",
        symbol="BTC/USDT:USDT",
        strategy_id="signal_arbiter_v1",
        strategy_params={
            "arbiter": {
                "min_net_edge_bps": 0.0,
                "taker_min_profit_factor": 1.35,
            },
            "strategies": [
                {
                    "strategy_id": "trend_continuation_v1",
                    "expected_edge_bps": 3.0,
                    "expected_cost_bps": 1.0,
                    "profit_factor": 1.2,
                },
                {
                    "strategy_id": "scalper_1m_v1",
                    "source_id": "scalper_fast_lane",
                    "expected_edge_bps": 6.0,
                    "expected_cost_bps": 2.0,
                    "profit_factor": 1.5,
                },
            ],
        },
    )

    strat = _build_strategy(
        spec, pd.DataFrame(columns=["timestamp", "funding_rate"]), feed=None
    )

    assert isinstance(strat, CompositeSignalStrategy)
    assert strat.strategy_id == "signal_arbiter_v1"
    assert [child.strategy_id for child in strat.strategies] == [
        "trend_continuation_v1",
        "scalper_1m_v1",
    ]
    assert strat.candidate_defaults["scalper_1m_v1#2"]["source_id"] == "scalper_fast_lane"


def test_build_strategy_rejects_unknown_id():
    import pandas as pd
    import pytest

    from vnedge.runtime.multi_lane import _build_strategy

    spec = LaneSpec(lane_id="x", exchange="bybit", symbol="XRP/USDT:USDT",
                    strategy_id="not_a_real_strategy_v9")
    with pytest.raises(ValueError, match="unsupported lane strategy_id"):
        _build_strategy(spec, pd.DataFrame(), feed=None)


def test_candidate_shadow_lanes_default_includes_xrp_trend():
    from vnedge.runtime.multi_lane_shadow import candidate_shadow_lanes

    lanes = candidate_shadow_lanes({})
    xrp = next(lane for lane in lanes
               if lane.lane_id == "trend_continuation_xrp_bybit_shadow")
    assert xrp.strategy_id == "trend_continuation_v1"
    assert xrp.symbol == "XRP/USDT:USDT"
    assert xrp.exchange == "bybit"
    assert xrp.mode is RunnerMode.SHADOW      # observe only, never a fill
    assert xrp.is_primary is False            # never the governed flat snapshot


def test_candidate_shadow_lanes_can_be_disabled():
    from vnedge.runtime.multi_lane_shadow import candidate_shadow_lanes

    assert candidate_shadow_lanes({"MULTI_LANE_CANDIDATES": "0"}) == []


# --- Delta native funding backfill wiring -----------------------------------------

def _delta_spec():
    return LaneSpec(lane_id="funding_mr_delta_india_btc_usd_usd_shadow",
                    exchange="delta_india", symbol="BTC/USD:USD",
                    strategy_id="funding_mean_reversion_v1")


def _empty_funding():
    import pandas as pd

    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "funding_rate": pd.Series(dtype="float64"),
        }
    )


async def test_delta_funding_seed_uses_native_backfill(monkeypatch):
    import pandas as pd

    import vnedge.data.delta_native_history as dnh

    backfill = pd.DataFrame({
        "timestamp": pd.to_datetime([1_000, 2_000], unit="s", utc=True),
        "funding_rate": [0.0001, -0.0002],
    })
    seen = {}

    async def fake_fetch(symbol, days=30, **kwargs):
        seen["symbol"], seen["days"] = symbol, days
        return backfill

    monkeypatch.setattr(dnh, "fetch_delta_funding_history", fake_fetch)
    out = await multi_lane._delta_funding_seed(_delta_spec(), _empty_funding())
    assert out is backfill
    assert seen == {"symbol": "BTC/USD:USD", "days": 30}


async def test_delta_funding_seed_falls_back_on_fetch_failure(monkeypatch):
    # failure posture: today's behaviour (empty seed -> live accumulation
    # behind the warmup mask); the backfill must never crash lane build
    import vnedge.data.delta_native_history as dnh

    async def boom(symbol, days=30, **kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr(dnh, "fetch_delta_funding_history", boom)
    fallback = _empty_funding()
    out = await multi_lane._delta_funding_seed(_delta_spec(), fallback)
    assert out is fallback


async def test_delta_funding_seed_falls_back_on_empty_backfill(monkeypatch):
    import vnedge.data.delta_native_history as dnh

    async def empty(symbol, days=30, **kwargs):
        return _empty_funding()

    monkeypatch.setattr(dnh, "fetch_delta_funding_history", empty)
    fallback = _empty_funding()
    out = await multi_lane._delta_funding_seed(_delta_spec(), fallback)
    assert out is fallback


async def test_delta_funding_seed_ignores_non_delta_exchanges(monkeypatch):
    import vnedge.data.delta_native_history as dnh

    called = []

    async def fake_fetch(symbol, days=30, **kwargs):
        called.append(symbol)
        return _empty_funding()

    monkeypatch.setattr(dnh, "fetch_delta_funding_history", fake_fetch)
    spec = LaneSpec(lane_id="x", exchange="binanceusdm", symbol="BTC/USDT:USDT",
                    strategy_id="funding_mean_reversion_v1")
    fallback = _empty_funding()
    out = await multi_lane._delta_funding_seed(spec, fallback)
    assert out is fallback
    assert called == []


def test_build_strategy_backfilled_seed_keeps_persistent_accumulator(tmp_path):
    # a NON-empty backfilled seed on a history-less venue must still get the
    # persistent accumulator (store keeps appending live prints on top)
    import pandas as pd

    from vnedge.runtime.funding_accumulator import LivePersistentFundingMR
    from vnedge.runtime.multi_lane import _build_strategy

    seed = pd.DataFrame({
        "timestamp": pd.to_datetime([1_000, 2_000], unit="s", utc=True),
        "funding_rate": [0.0001, -0.0002],
    })

    class _Feed:
        exchange_id = "delta_india"
        funding_rate = 0.0001

    strat = _build_strategy(
        _delta_spec(), seed, feed=_Feed(),
        funding_store_path=tmp_path / "lane.funding.jsonl",
    )
    assert isinstance(strat, LivePersistentFundingMR)
    assert len(strat.funding) == 2  # seeded, not the synthetic 1970 anchor
