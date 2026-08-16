"""HF signal layer — SignalIntent, TickSnapshot, the two engines, regime filter,
and the CostGate bridge."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from vnedge.risk.cost_gate import CostGate, CostProfile
from vnedge.strategy.hf_regime import RangingRegimeFilter
from vnedge.strategy.mean_reversion_engine import ShortTermMeanReversionEngine, build_hf_engines
from vnedge.strategy.signal_engine import (
    OrderFlowImbalanceEngine,
    SignalIntent,
    TickSnapshot,
    cost_gate_intent,
    make_signal_id,
)


def _tick(mid, sec, sym="BTCUSDT", imb=None, spread="0.02"):
    m = Decimal(str(mid))
    half = Decimal(spread) / 2
    return TickSnapshot(
        symbol=sym, ts=datetime(2026, 1, 1, 0, 0, sec, tzinfo=UTC),
        last_price=m, bid=m - half, ask=m + half,
        bid_size=Decimal("1"), ask_size=Decimal("1"),
        trade_imbalance_1s=None if imb is None else Decimal(str(imb)),
    )


# --- SignalIntent / TickSnapshot / id -----------------------------------------

def test_signal_intent_validates_side_and_urgency():
    ok = dict(symbol="BTCUSDT", side="buy", stop_distance_bps=Decimal("10"),
              urgency="maker", edge_estimate_bps=Decimal("9"), expected_holding_seconds=30,
              signal_id="x", ts=datetime(2026, 1, 1, tzinfo=UTC))
    SignalIntent(**ok)                                  # valid
    with pytest.raises(Exception):
        SignalIntent(**{**ok, "side": "long"})          # bad side
    with pytest.raises(Exception):
        SignalIntent(**{**ok, "urgency": "instant"})    # bad urgency


def test_tick_snapshot_computes_mid():
    t = _tick("100.00", 0, spread="0.10")
    assert t.mid == Decimal("100.00")   # (99.95 + 100.05) / 2


def test_signal_id_is_deterministic():
    a = make_signal_id("ofi", "BTCUSDT", datetime(2026, 1, 1, tzinfo=UTC), "buy")
    b = make_signal_id("ofi", "BTCUSDT", datetime(2026, 1, 1, tzinfo=UTC), "buy")
    c = make_signal_id("ofi", "BTCUSDT", datetime(2026, 1, 1, tzinfo=UTC), "sell")
    assert a == b and a != c


# --- OrderFlowImbalanceEngine -------------------------------------------------

def test_ofi_fires_on_strong_imbalance():
    eng = OrderFlowImbalanceEngine(symbol="BTCUSDT")
    out = eng.generate(_tick("100.01", 0, imb="0.8"), Decimal("500"), [])
    assert len(out) == 1
    s = out[0]
    assert s.side == "buy" and s.urgency == "taker" and s.edge_estimate_bps >= Decimal("8")


def test_ofi_silent_on_weak_imbalance_and_when_positioned():
    eng = OrderFlowImbalanceEngine(symbol="BTCUSDT")
    assert eng.generate(_tick("100.01", 0, imb="0.1"), Decimal("500"), []) == []
    eng2 = OrderFlowImbalanceEngine(symbol="BTCUSDT")
    assert eng2.generate(_tick("100.01", 0, imb="0.8"), Decimal("500"),
                         [{"symbol": "BTCUSDT"}]) == []   # flat-only


def test_ofi_cooldown_blocks_within_window():
    eng = OrderFlowImbalanceEngine(symbol="BTCUSDT")
    assert len(eng.generate(_tick("100.01", 0, imb="0.8"), Decimal("500"), [])) == 1
    assert eng.generate(_tick("100.01", 2, imb="0.8"), Decimal("500"), []) == []  # 2s < 3s


def test_ofi_deterministic_across_fresh_engines():
    t = _tick("100.01", 0, imb="0.8")
    a = OrderFlowImbalanceEngine().generate(t, Decimal("500"), [])[0]
    b = OrderFlowImbalanceEngine().generate(t, Decimal("500"), [])[0]
    assert a.signal_id == b.signal_id   # no random uuid


# --- RangingRegimeFilter ------------------------------------------------------

def test_regime_warmup_then_ranging_vs_trending():
    f = RangingRegimeFilter(lookback=10)
    for _ in range(5):
        assert f.update(Decimal("100")).reason == "warming_up"
    # a choppy series: large path, tiny net move → ranging
    chop = RangingRegimeFilter(lookback=10)
    st = None
    for i in range(30):
        st = chop.update(Decimal("100.4") if i % 2 else Decimal("99.6"))
    assert st.is_ranging is True
    # a monotonic trend: net move == path → ER≈1 → not ranging
    trend = RangingRegimeFilter(lookback=10)
    st = None
    for i in range(30):
        st = trend.update(Decimal("100") + Decimal(i) / 10)
    assert st.is_ranging is False


# --- ShortTermMeanReversionEngine --------------------------------------------

def test_mr_emits_in_ranging_deviation():
    eng = ShortTermMeanReversionEngine(symbol="BTCUSDT")
    fired = []
    for i in range(40):                       # oscillate ±0.4 around 100 (ranging), end low
        mid = "100.40" if i % 2 else "99.60"
        fired += list(eng.generate(_tick(mid, i, spread="0.02"), Decimal("500"), []))
    assert fired, "expected at least one mean-reversion signal in a ranging series"
    assert all(s.side in ("buy", "sell") for s in fired)


def test_mr_hard_blocked_in_trend():
    eng = ShortTermMeanReversionEngine(symbol="BTCUSDT")
    fired = []
    for i in range(40):                       # monotonic uptrend → regime blocks
        fired += list(eng.generate(_tick(100 + i * 0.1, i % 60, spread="0.02"),
                                   Decimal("500"), []))
    assert fired == []


def test_build_hf_engines_roster():
    engines = build_hf_engines(("BTCUSDT",))
    ids = {e.engine_id for e in engines}
    assert ids == {"OrderFlowImbalanceEngine", "ShortTermMeanReversionEngine"}


# --- CostGate bridge ----------------------------------------------------------

def test_cost_gate_bridge_rejects_weak_and_approves_strong():
    gate = CostGate(CostProfile.SCALP, min_net_edge_bps=Decimal("4.0"))
    weak = SignalIntent(symbol="BTCUSDT", side="buy", stop_distance_bps=Decimal("10"),
                        urgency="taker", edge_estimate_bps=Decimal("5"),
                        expected_holding_seconds=30, signal_id="w", ts=datetime(2026, 1, 1, tzinfo=UTC))
    strong = weak.model_copy(update={"edge_estimate_bps": Decimal("25"), "urgency": "maker"})
    assert cost_gate_intent(weak, gate, 0).approved is False     # 5bps < ~14bps taker cost
    assert cost_gate_intent(strong, gate, 0).approved is True    # 25bps clears ~10bps maker cost
