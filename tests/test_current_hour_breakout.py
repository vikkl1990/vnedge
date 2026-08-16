"""Current-hour breakout engine."""

from datetime import UTC, datetime
from decimal import Decimal

from vnedge.strategy.current_hour_breakout import CurrentHourBreakoutEngine
from vnedge.strategy.signal_engine import TickSnapshot


def _tk(mid, hour, minute=0, sec=0):
    m = Decimal(str(mid))
    return TickSnapshot(symbol="ETHUSDT", ts=datetime(2026, 1, 1, hour, minute, sec, tzinfo=UTC),
                        last_price=m, bid=m - Decimal("0.01"), ask=m + Decimal("0.01"),
                        bid_size=Decimal("1"), ask_size=Decimal("1"))


def _eng():
    return CurrentHourBreakoutEngine(symbol="ETHUSDT", min_elapsed_min=15,
                                     min_range_bps=Decimal("40"), break_buffer_bps=Decimal("3"),
                                     min_edge_bps=Decimal("10"), active_hours=(12,))


def test_fires_after_min_time_and_range():
    eng = _eng()
    out = []
    for mid, minute in [(100.0, 0), (100.5, 5), (100.0, 10), (100.4, 14)]:   # build ~50bps range, too young
        out += list(eng.generate(_tk(mid, 12, minute), Decimal("500"), []))
    assert out == []                                                          # nothing before min time
    out += list(eng.generate(_tk(100.7, 12, 16), Decimal("500"), []))        # break running high after 15m
    assert any(s.side == "buy" for s in out)


def test_no_break_while_hour_too_young():
    eng = _eng()
    out = []
    for mid, minute in [(100.0, 0), (100.5, 3), (100.9, 5)]:   # break at 5min — too young
        out += list(eng.generate(_tk(mid, 12, minute), Decimal("500"), []))
    assert out == []
