"""Range-breakout engine — session + range gated."""

from datetime import UTC, datetime
from decimal import Decimal

from vnedge.strategy.breakout_engine import RangeBreakoutEngine
from vnedge.strategy.signal_engine import TickSnapshot


def _tk(mid, sec, hour=12):
    m = Decimal(str(mid))
    return TickSnapshot(symbol="BTCUSDT", ts=datetime(2026, 1, 1, hour, 0, sec, tzinfo=UTC),
                        last_price=m, bid=m - Decimal("0.01"), ask=m + Decimal("0.01"),
                        bid_size=Decimal("1"), ask_size=Decimal("1"))


def _eng(**kw):
    return RangeBreakoutEngine(symbol="BTCUSDT", window_sec=100, min_range_bps=Decimal("10"),
                               active_hours=(12,), min_window_pts=5, cooldown_sec=0, **kw)


def test_breakout_fires_on_range_break_in_session():
    eng = _eng()
    out = []
    for i, p in enumerate([100.0, 100.1, 99.9, 100.1, 99.9, 100.1, 99.9, 100.05, 100.0, 100.1]):
        out += list(eng.generate(_tk(p, i), Decimal("500"), []))
    out += list(eng.generate(_tk(100.5, 11), Decimal("500"), []))   # break the 100.1 high
    assert any(s.side == "buy" for s in out)


def test_breakout_blocked_outside_active_session():
    eng = _eng()
    out = []
    for i, p in enumerate([100.0, 100.1, 99.9, 100.1, 99.9, 100.1, 99.9, 100.05, 100.0, 100.1, 100.5]):
        out += list(eng.generate(_tk(p, i, hour=3), Decimal("500"), []))   # hour 3 ∉ session
    assert out == []
