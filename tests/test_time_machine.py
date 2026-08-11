from datetime import UTC, datetime, timedelta

from vnedge.data.time_machine import TimeMachine, TimeMachineConfig

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _k(open_time, o, h, l, c, v, ex_ts=None):
    return {"open_time": open_time, "open": o, "high": h, "low": l, "close": c,
            "volume": v, "exchange_ts": ex_ts or open_time}


def test_forming_then_closed_transition():
    tm = TimeMachine(["BTC"], ["1m"])
    tm.on_kline_update("BTC", "1m", _k(BASE, 100, 101, 99, 100, 5, BASE + timedelta(seconds=30)), False)
    f = tm.get_forming("BTC", "1m")
    assert f is not None and not f.is_closed and 0 < f.forming_progress < 1
    tm.on_kline_update("BTC", "1m", _k(BASE, 100, 102, 99, 101, 10, BASE + timedelta(seconds=60)), True)
    assert tm.get_forming("BTC", "1m") is None
    lc = tm.get_last_closed("BTC", "1m")
    assert lc.is_closed and lc.close == 101 and lc.forming_progress == 0.0


def test_new_open_finalizes_previous_forming():
    tm = TimeMachine(["BTC"], ["1m"])
    closed = []
    tm.subscribe("closed_bar", lambda s, tf, c: closed.append(c))
    tm.on_kline_update("BTC", "1m", _k(BASE, 100, 101, 99, 100, 5, BASE + timedelta(seconds=30)), False)
    t1 = BASE + timedelta(minutes=1)
    tm.on_kline_update("BTC", "1m", _k(t1, 101, 101, 100, 101, 3, t1 + timedelta(seconds=5)), False)
    assert len(closed) == 1 and closed[0].open_time == BASE and closed[0].is_closed
    assert tm.get_forming("BTC", "1m").open_time == t1


def test_monotonicity_drops_backward_bars():
    tm = TimeMachine(["BTC"], ["1m"])
    tm.on_kline_update("BTC", "1m", _k(BASE, 100, 101, 99, 100, 5), True)
    seq = tm.get_last_closed("BTC", "1m").sequence_id
    tm.on_kline_update("BTC", "1m", _k(BASE - timedelta(minutes=1), 1, 1, 1, 1, 1), True)
    assert tm.get_last_closed("BTC", "1m").sequence_id == seq   # unchanged


def test_gap_detection_emits_and_marks_health():
    tm = TimeMachine(["BTC"], ["1m"])
    gaps = []
    tm.subscribe("gap", lambda s, tf, info: gaps.append(info))
    tm.on_kline_update("BTC", "1m", _k(BASE, 100, 101, 99, 100, 5), True)
    t5 = BASE + timedelta(minutes=5)
    tm.on_kline_update("BTC", "1m", _k(t5, 100, 101, 99, 100, 5, t5), True)
    assert len(gaps) == 1 and gaps[0]["missing"] == 4
    assert tm.get_state("BTC").health["1m"] == "gapped"


def test_future_bar_rejected():
    tm = TimeMachine(["BTC"], ["1m"])
    tm.on_kline_update("BTC", "1m", _k(BASE + timedelta(minutes=10), 100, 101, 99, 100, 5, BASE), False)
    assert tm.get_forming("BTC", "1m") is None
    assert tm.get_state("BTC").health["1m"] == "future"


def test_stall_detection():
    tm = TimeMachine(["BTC"], ["1m"])
    stalls = []
    tm.subscribe("stall", lambda s, tf: stalls.append((s, tf)))
    tm.on_kline_update("BTC", "1m", _k(BASE, 100, 101, 99, 100, 5, BASE), True)
    tm.check_health(BASE + timedelta(minutes=5))   # > 2.5 × 1m
    assert ("BTC", "1m") in stalls
    assert tm.get_state("BTC").health["1m"] == "stale"


def test_forming_updates_are_throttled():
    tm = TimeMachine(["BTC"], ["1m"], TimeMachineConfig(forming_update_throttle_ms=500))
    updates = []
    tm.subscribe("forming_update", lambda s, tf, c: updates.append(c))
    tm.on_kline_update("BTC", "1m", _k(BASE, 100, 100, 100, 100, 1, BASE + timedelta(milliseconds=100)), False)
    tm.on_kline_update("BTC", "1m", _k(BASE, 100, 101, 100, 101, 2, BASE + timedelta(milliseconds=300)), False)
    assert len(updates) == 1                          # 2nd throttled (200ms < 500ms)
    tm.on_kline_update("BTC", "1m", _k(BASE, 100, 101, 99, 100, 3, BASE + timedelta(milliseconds=700)), False)
    assert len(updates) == 2                          # 600ms since emit → allowed


def test_trade_aggregation_reconstructs_1m_ohlc():
    tm = TimeMachine(["BTC"], ["1m"])
    for i, (px, sz) in enumerate([(100, 1), (105, 2), (98, 1), (102, 3)]):
        tm.on_trade("BTC", px, sz, BASE + timedelta(seconds=10 * i))
    f = tm.get_forming("BTC", "1m")
    assert (f.open, f.high, f.low, f.close, f.volume) == (100, 105, 98, 102, 7)


def test_snapshot_dict_shape():
    tm = TimeMachine(["BTC"], ["1m", "1h"])
    tm.on_kline_update("BTC", "1m", _k(BASE, 100, 101, 99, 100, 5, BASE + timedelta(seconds=30)), False)
    d = tm.snapshot_dict("BTC")
    assert "1m" in d["forming"] and 0 <= d["forming"]["1m"]["progress"] < 1
    assert d["health"]["1m"] == "ok"
