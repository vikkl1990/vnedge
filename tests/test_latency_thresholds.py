from vnedge.runtime import latency_thresholds as LT


def test_tm_age_bands_1m():
    assert LT.classify_tm_age("1m", last_ms=300) == "ok"            # < soft 1500
    assert LT.classify_tm_age("1m", last_ms=2000) == "soft"          # > soft, < hard-last 5000
    assert LT.classify_tm_age("1m", last_ms=6000) == "hard"          # > hard-last 5000
    assert LT.classify_tm_age("1m", last_ms=300, p99_ms=3500) == "hard"  # p99 > hard-p99 3000


def test_tm_age_decision_tf_1h():
    # 1h is the decision TF for funding-style lanes: last-sample hard is 90s
    assert LT.classify_tm_age("1h", last_ms=7000) == "ok"           # < soft 8000
    assert LT.classify_tm_age("1h", last_ms=9000) == "soft"          # > soft, < hard 90000
    assert LT.classify_tm_age("1h", last_ms=95000) == "hard"


def test_unknown_and_unconfigured():
    assert LT.classify_tm_age("1m", last_ms=None) == "unknown"
    # an unconfigured TF has no budget -> never escalates past ok (fail-open on
    # classification is safe here; the arm-gate only blocks on decision TF)
    assert LT.classify_tm_age("30m", last_ms=999999) == "ok"


def test_classify_p99_generic():
    assert LT.classify_p99(400, LT.CLOSED_BAR_LAG_SOFT_P99_MS, LT.CLOSED_BAR_LAG_HARD_P99_MS) == "ok"
    assert LT.classify_p99(900, LT.CLOSED_BAR_LAG_SOFT_P99_MS, LT.CLOSED_BAR_LAG_HARD_P99_MS) == "soft"
    assert LT.classify_p99(2500, LT.CLOSED_BAR_LAG_SOFT_P99_MS, LT.CLOSED_BAR_LAG_HARD_P99_MS) == "hard"
    assert LT.classify_p99(None, 500, 2000) == "unknown"


def test_closed_bar_delivery_budget_scales_with_causal_timeframe():
    assert LT.closed_bar_receipt_limits("1m") == (500, 2000, 1500)
    assert LT.closed_bar_receipt_limits("5m") == (2000, 5000, 4000)
    assert LT.closed_bar_receipt_limits("15m") == (3000, 8000, 6000)
    assert LT.closed_bar_receipt_limits("1h") == (5000, 15000, 10000)
    assert LT.closed_bar_receipt_limits("unknown") == (500, 2000, 1500)


def test_arm_gate_only_hard_blocks():
    assert LT.blocks_new_arms("hard") is True
    assert LT.blocks_new_arms("soft") is False
    assert LT.blocks_new_arms("ok") is False
    assert LT.blocks_new_arms("unknown") is False
