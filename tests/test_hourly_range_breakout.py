"""Hourly-range breakout engine."""

from datetime import UTC, datetime
from decimal import Decimal

from vnedge.strategy.hourly_range_breakout import HourlyRangeBreakoutEngine
from vnedge.strategy.signal_engine import TickSnapshot


def _tk(mid, hour, minute=0, sec=0):
    m = Decimal(str(mid))
    return TickSnapshot(symbol="ETHUSDT", ts=datetime(2026, 1, 1, hour, minute, sec, tzinfo=UTC),
                        last_price=m, bid=m - Decimal("0.01"), ask=m + Decimal("0.01"),
                        bid_size=Decimal("1"), ask_size=Decimal("1"))


def test_fires_on_break_of_prev_hour_high():
    eng = HourlyRangeBreakoutEngine(symbol="ETHUSDT", min_range_bps=Decimal("50"),
                                    break_buffer_bps=Decimal("3"), min_edge_bps=Decimal("10"),
                                    active_hours=(12, 13))
    out = []
    for mid, minute in [(100.0, 0), (100.6, 15), (100.0, 30), (100.3, 45)]:   # hour 12: ~60bps range
        out += list(eng.generate(_tk(mid, 12, minute), Decimal("500"), []))
    out += list(eng.generate(_tk(100.8, 13, 0), Decimal("500"), []))          # break prev-hour high
    assert any(s.side == "buy" for s in out)
    assert not any(s for s in out if s.ts.hour == 12)   # no signal before a finalized prior hour


def test_blocked_outside_session():
    eng = HourlyRangeBreakoutEngine(symbol="ETHUSDT", min_range_bps=Decimal("50"),
                                    active_hours=(99,))   # never active
    out = []
    for mid, minute in [(100.0, 0), (100.6, 15), (100.0, 30)]:
        out += list(eng.generate(_tk(mid, 12, minute), Decimal("500"), []))
    out += list(eng.generate(_tk(100.8, 13, 0), Decimal("500"), []))
    assert out == []
