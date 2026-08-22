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
from vnedge.runtime.latency_tracker import BAR_CLOSE_PROCESSING_MS, LatencyTracker
from vnedge.runtime.live_paper import LivePaperSession

BASE = datetime(2026, 1, 1, tzinfo=UTC)
_gate = LivePaperSession._candle_path_arm_block  # unbound; call with a stub self


def _k(open_time, c=100.0, ex_ts=None):
    return {
        "open_time": open_time,
        "open": c,
        "high": c,
        "low": c,
        "close": c,
        "volume": 1.0,
        "exchange_ts": ex_ts or open_time,
    }


class _Cfg:
    def __init__(self, tf="1h", symbol="BTC/USDT"):
        self.timeframe = tf
        self.symbol = symbol


class _Stub:
    """Minimal object exposing only what _candle_path_arm_block touches."""

    def __init__(self, tm, *, degraded=False, tf="1h", latency=None):
        self.time_machine = tm
        self._tm_degraded = degraded
        self.config = _Cfg(tf=tf)
        self.latency = latency


# --- Time Machine accessors --------------------------------------------------
def test_health_of_and_age_ms():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    assert tm.health_of("BTC/USDT", "1h") == "ok"  # never-updated reads ok
    assert tm.age_ms("BTC/USDT", "1h", BASE) is None  # no update yet
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE, ex_ts=BASE + timedelta(seconds=30)), False)
    age = tm.age_ms("BTC/USDT", "1h", BASE + timedelta(seconds=90))
    assert abs(age - 60_000) < 1.0  # 90s - 30s = 60s


def test_snapshot_dict_age_block():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE, ex_ts=BASE + timedelta(seconds=10)), False)
    d = tm.snapshot_dict("BTC/USDT", now=BASE + timedelta(seconds=40))
    assert "age_ms" in d and abs(d["age_ms"]["1h"] - 30_000) < 1.0
    assert "age_ms" not in tm.snapshot_dict("BTC/USDT")  # omitted without now


# --- gate: allow -------------------------------------------------------------
def test_gate_allows_when_healthy_and_fresh():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE, ex_ts=BASE + timedelta(seconds=30)), False)
    # fresh: 1s after last update, health ok
    assert _gate(_Stub(tm), BASE + timedelta(seconds=31)) is None


def test_gate_noop_when_no_time_machine():
    assert _gate(_Stub(None), BASE) is None  # unsupported TF -> no gate


# --- gate: block reasons -----------------------------------------------------
def test_gate_blocks_on_tm_error():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE), False)
    assert _gate(_Stub(tm, degraded=True), BASE) == "tm_error"


def test_gate_blocks_on_stale_decision_tf():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE, ex_ts=BASE), False)
    later = BASE + timedelta(hours=3)  # > 2.5x 1h stall
    tm.check_health(later)
    assert tm.health_of("BTC/USDT", "1h") == "stale"
    assert _gate(_Stub(tm), later) == "decision_tf_stale"


def test_gate_blocks_on_future_bar():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    # bar opens materially after exchange_ts -> future health
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE + timedelta(hours=1), ex_ts=BASE), False)
    assert tm.health_of("BTC/USDT", "1h") == "future"
    assert _gate(_Stub(tm), BASE) == "decision_tf_future"


def test_gate_blocks_on_hard_age_while_health_ok():
    # age can breach the 90s HARD budget long before the 2.5h stall flips health
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE, ex_ts=BASE), False)
    now = BASE + timedelta(milliseconds=LT.TM_AGE_HARD_LAST_MS["1h"] + 1_000)
    assert tm.health_of("BTC/USDT", "1h") == "ok"  # not stale yet
    assert _gate(_Stub(tm), now) == "tm_age_hard"


def test_gate_faults_fail_closed(monkeypatch):
    # Unknown health must never arm new risk.
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE), False)

    def boom(*a, **k):
        raise RuntimeError("sensor down")

    monkeypatch.setattr(tm, "health_of", boom)
    assert _gate(_Stub(tm), BASE) == "tm_error"


def test_gate_blocks_hard_closed_bar_processing_lag():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE), False)
    latency = LatencyTracker()
    for _ in range(LT.LATENCY_GATE_MIN_SAMPLES):
        latency.record(BAR_CLOSE_PROCESSING_MS, LT.CLOSED_BAR_LAG_HARD_P99_MS + 1)

    assert _gate(_Stub(tm, latency=latency), BASE) == "bar_close_lag_hard"


def test_gate_blocks_hard_decision_compute_lag():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE), False)
    latency = LatencyTracker()
    _, hard_ms, _ = LT.decision_compute_limits("1h")
    for _ in range(LT.LATENCY_GATE_MIN_SAMPLES):
        latency.record("decision_lag_ms", hard_ms + 1)

    assert _gate(_Stub(tm, latency=latency), BASE) == "decision_compute_hard"


def test_gate_uses_decision_timeframe_compute_budget():
    tm = TimeMachine(["BTC/USDT"], ["5m", "15m"])
    for timeframe in ("5m", "15m"):
        tm.on_kline_update("BTC/USDT", timeframe, _k(BASE), False)
    latency = LatencyTracker()
    # 600ms is unsafe for a 5m scanner but inside the explicitly bounded 15m
    # structure budget. The candle-arrival metric remains independently gated.
    for _ in range(LT.LATENCY_GATE_MIN_SAMPLES):
        latency.record("decision_lag_ms", 600)

    assert _gate(_Stub(tm, latency=latency, tf="5m"), BASE) == "decision_compute_hard"
    assert _gate(_Stub(tm, latency=latency, tf="15m"), BASE) is None


def test_gate_self_recovers_after_five_fresh_healthy_bar_closes():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE), False)
    latency = LatencyTracker()
    for _ in range(LT.LATENCY_GATE_MIN_SAMPLES):
        latency.record(BAR_CLOSE_PROCESSING_MS, LT.CLOSED_BAR_LAG_HARD_P99_MS + 1)
    for _ in range(LT.LATENCY_RECOVERY_CONSECUTIVE_SAMPLES):
        latency.record(BAR_CLOSE_PROCESSING_MS, LT.CLOSED_BAR_LAG_RECOVERY_MS)

    stats = latency.stats(BAR_CLOSE_PROCESSING_MS)
    assert stats["p95"] > LT.CLOSED_BAR_LAG_HARD_P99_MS
    assert _gate(_Stub(tm, latency=latency), BASE) is None


def test_gate_stays_blocked_until_recovery_proof_is_complete():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE), False)
    latency = LatencyTracker()
    for _ in range(LT.LATENCY_GATE_MIN_SAMPLES):
        latency.record(BAR_CLOSE_PROCESSING_MS, LT.CLOSED_BAR_LAG_HARD_P99_MS + 1)
    for _ in range(LT.LATENCY_RECOVERY_CONSECUTIVE_SAMPLES - 1):
        latency.record(BAR_CLOSE_PROCESSING_MS, LT.CLOSED_BAR_LAG_RECOVERY_MS)

    assert _gate(_Stub(tm, latency=latency), BASE) == "bar_close_lag_hard"


def test_gate_reblocks_immediately_when_recovery_relapses():
    tm = TimeMachine(["BTC/USDT"], ["1h"])
    tm.on_kline_update("BTC/USDT", "1h", _k(BASE), False)
    latency = LatencyTracker()
    for _ in range(LT.LATENCY_GATE_MIN_SAMPLES):
        latency.record(BAR_CLOSE_PROCESSING_MS, LT.CLOSED_BAR_LAG_HARD_P99_MS + 1)
    for _ in range(LT.LATENCY_RECOVERY_CONSECUTIVE_SAMPLES):
        latency.record(BAR_CLOSE_PROCESSING_MS, LT.CLOSED_BAR_LAG_RECOVERY_MS)
    assert _gate(_Stub(tm, latency=latency), BASE) is None

    latency.record(BAR_CLOSE_PROCESSING_MS, LT.CLOSED_BAR_LAG_RECOVERY_MS + 1)
    assert _gate(_Stub(tm, latency=latency), BASE) == "bar_close_lag_hard"
