"""Phase B — candle-path composite arm-gate + Time Machine health/age accessors.

The gate blocks a NEW entry when the decision timeframe is unsafe to arm on. It
is consulted ONLY on the entry path (`if self._plan is None ...`), downstream of
`_manage_exit`, so exits/reduce-only are structurally unreachable by it — the
tests below verify the gate's block/allow logic in isolation; the exit-safety is
a property of the call site (documented at the gate and its insertion point).
"""
from datetime import UTC, datetime, timedelta

from vnedge.data.time_machine import TimeMachine
from vnedge.runtime import latency_thresholds as LT
from vnedge.runtime.live_paper import LivePaperSession

BASE = datetime(2026, 1, 1, tzinfo=UTC)
_gate = LivePaperSession._candle_path_arm_block  # unbound; call with a stub self


def _k(open_time, c=100.0, ex_ts=None):
    return {"open_time": open_time, "open": c, "high": c, "low": c, "close": c,
            "volume": 1.0, "exchange_ts": ex_ts or open_time}


class _Cfg:
    def __init__(self, tf="1h", symbol="BTC/USDT"):
        self.timeframe = tf
        self.symbol = symbol


class _Stub:
    """Minimal object exposing only what _candle_path_arm_block touches."""
    def __init__(self, tm, *, degraded=False, tf="1h"):
        self.time_machine = tm
        self._tm_degraded = degraded
        self.config = _Cfg(tf=tf)


# --- Time Machine accessors --------------------------------------------------
def test_health_of_and_age_ms():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    assert tm.health_of("BTC/USDT", "1h") == "ok"       # never-updated reads ok
    assert tm.age_ms("BTC/USDT", "1h", BASE) is None     # no update yet
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE, ex_ts=BASE + timedelta(seconds=30)), False)
    age = tm.age_ms("BTC/USDT", "1h", BASE + timedelta(seconds=90))
    assert abs(age - 60_000) < 1.0                        # 90s - 30s = 60s


def test_snapshot_dict_age_block():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE, ex_ts=BASE + timedelta(seconds=10)), False)
    d = tm.snapshot_dict("BTC/USDT", now=BASE + timedelta(seconds=40))
    assert "age_ms" in d and abs(d["age_ms"]["1h"] - 30_000) < 1.0
    assert "age_ms" not in tm.snapshot_dict("BTC/USDT")   # omitted without now


# --- gate: allow -------------------------------------------------------------
def test_gate_allows_when_healthy_and_fresh():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE, ex_ts=BASE + timedelta(seconds=30)), False)
    # fresh: 1s after last update, health ok
    assert _gate(_Stub(tm), BASE + timedelta(seconds=31)) is None


def test_gate_noop_when_no_time_machine():
    assert _gate(_Stub(None), BASE) is None                # unsupported TF -> no gate


# --- gate: block reasons -----------------------------------------------------
def test_gate_blocks_on_tm_error():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE), False)
    assert _gate(_Stub(tm, degraded=True), BASE) == "tm_error"


def test_gate_blocks_on_stale_decision_tf():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE, ex_ts=BASE), False)
    later = BASE + timedelta(hours=3)                      # > 2.5x 1h stall
    tm.check_health(later)
    assert tm.health_of("BTC/USDT", "1h") == "stale"
    assert _gate(_Stub(tm), later) == "decision_tf_stale"


def test_gate_blocks_on_future_bar():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    # bar opens materially after exchange_ts -> future health
    tm.on_kline_update("BTC/USDT", "1h",
                       _k(BASE + timedelta(hours=1), ex_ts=BASE), False)
    assert tm.health_of("BTC/USDT", "1h") == "future"
    assert _gate(_Stub(tm), BASE) == "decision_tf_future"


def test_gate_blocks_on_hard_age_while_health_ok():
    # age can breach the 90s HARD budget long before the 2.5h stall flips health
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE, ex_ts=BASE), False)
    now = BASE + timedelta(milliseconds=LT.TM_AGE_HARD_LAST_MS["1h"] + 1_000)
    assert tm.health_of("BTC/USDT", "1h") == "ok"          # not stale yet
    assert _gate(_Stub(tm), now) == "tm_age_hard"


def test_gate_faults_fail_closed(monkeypatch):
    # Unknown health must never arm new risk.
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE), False)

    def boom(*a, **k):
        raise RuntimeError("sensor down")
    monkeypatch.setattr(tm, "health_of", boom)
    assert _gate(_Stub(tm), BASE) == "tm_error"
