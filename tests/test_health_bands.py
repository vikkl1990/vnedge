"""Server-side health bands/chips — the single source both cockpits render."""

from vnedge.dashboard.health_bands import annotate, compute_chips, lane_bands


def _lane(**kw):
    d = {
        "timeframe": "1h",
        "time_machine": {"health": {"1h": "ok"}, "age_ms": {"1h": 400}},
        "latency": {
            "bar_close_processing_ms": {"p95": 25, "n": 20},
            "decision_lag_ms": {"p95": 25, "n": 20},
        },
        "decision_skips": {},
    }
    d.update(kw)
    return d


def test_all_ok_chips():
    c = compute_chips(
        {
            "lanes": [_lane()],
            "feed_health": {"candles": "ok"},
            "risk_status": "ok",
            "consecutive_losses": 0,
        }
    )
    assert c["SYSTEM"]["band"] == "ok" and c["CANDLE"]["band"] == "ok"
    assert c["FEED"]["band"] == "ok" and c["DECISION"]["band"] == "ok"


def test_risk_streak_degrades_and_rolls_into_system():
    c = compute_chips(
        {"lanes": [_lane()], "feed_health": {"candles": "ok"}, "consecutive_losses": 3}
    )
    assert c["RISK"]["band"] == "degraded" and c["SYSTEM"]["band"] == "degraded"


def test_candle_blocked_on_stale_decision_tf():
    l = _lane(time_machine={"health": {"1h": "stale"}, "age_ms": {"1h": 999999}})
    c = compute_chips({"lanes": [l], "feed_health": {"candles": "ok"}})
    assert c["CANDLE"]["band"] == "blocked" and c["SYSTEM"]["band"] == "blocked"


def test_raw_closed_bar_age_does_not_block_when_next_close_is_not_overdue():
    l = _lane(
        time_machine={
            "health": {"1h": "ok"},
            "age_ms": {"1h": 3_000_000},
            "closed_bar_overdue_ms": {"1h": 0},
        }
    )
    c = compute_chips({"lanes": [l], "feed_health": {"candles": "ok"}})
    assert c["CANDLE"] == {"band": "ok", "label": "ok"}
    assert c["SYSTEM"] == {"band": "ok", "label": "nominal"}


def test_decision_blocked_on_current_arm_block():
    l = _lane(arm_blocked="decision_tf_stale")
    c = compute_chips({"lanes": [l], "feed_health": {"candles": "ok"}})
    assert c["DECISION"]["band"] == "blocked" and c["CANDLE"]["band"] == "blocked"


def test_latency_arm_block_does_not_relabel_fresh_candle():
    l = _lane(arm_blocked="bar_close_lag_hard")
    c = compute_chips({"lanes": [l], "feed_health": {"candles": "ok"}})

    assert c["CANDLE"] == {"band": "ok", "label": "ok"}
    assert c["DECISION"] == {"band": "blocked", "label": "new arms blocked"}


def test_measurement_only_block_does_not_block_active_scanner_lanes():
    measurement = _lane(
        strategy_id="measurement_only_v1",
        observation_class="measurement",
        arm_blocked="lane_degraded:canonical_bar_timeout",
    )
    scanner = _lane(
        strategy_id="structure_bos_15m_trigger_v3",
        observation_class="shadow_observe",
    )
    chips = compute_chips(
        {
            "lanes": [measurement, scanner],
            "feed_health": {"candles": "ok"},
        }
    )

    assert chips["CANDLE"] == {"band": "ok", "label": "ok"}
    assert chips["DECISION"] == {"band": "ok", "label": "ok"}
    assert chips["SYSTEM"] == {"band": "ok", "label": "nominal"}


def test_cumulative_skips_alone_do_not_stick_blocked():
    # O2: decision_skips is a cumulative metric; the chip reflects CURRENT state
    # (arm_blocked), so a lane that hiccupped earlier but is fine now reads ok.
    l = _lane(decision_skips={"decision_tf_stale": 5}, arm_blocked=None)
    c = compute_chips({"lanes": [l], "feed_health": {"candles": "ok"}})
    assert c["DECISION"]["band"] == "ok" and c["CANDLE"]["band"] == "ok"


def test_kill_dominates_system_and_risk():
    c = compute_chips(
        {"lanes": [_lane()], "kill_switch_active": True, "feed_health": {"candles": "ok"}}
    )
    assert c["SYSTEM"]["band"] == "blocked" and c["RISK"]["band"] == "blocked"


def test_unknown_never_fakes_ok():
    c = compute_chips({"lanes": [], "feed_health": {}})
    assert c["CANDLE"]["band"] == "unknown" and c["DECISION"]["band"] == "unknown"
    assert c["SYSTEM"] == {"band": "unknown", "label": "incomplete telemetry"}


def test_lane_bands_drawdown():
    assert lane_bands(_lane(drawdown_pct=7.35, dd_limit_pct=6.0))["dd"] == "blocked"
    assert lane_bands(_lane(drawdown_pct=5.0, dd_limit_pct=6.0))["dd"] == "degraded"  # >= 0.8*6
    assert lane_bands(_lane(drawdown_pct=2.0, dd_limit_pct=6.0))["dd"] == "ok"


def test_lane_bands_verdict_tone():
    assert lane_bands(_lane(trial_scorecard={"verdict": "FAIL"}))["verdict_tone"] == "blocked"
    assert lane_bands(_lane(trial_scorecard={"verdict": "PENDING"}))["verdict_tone"] == "degraded"


def test_bar_close_latency_has_its_own_band_and_degrades_decision_chip():
    lane = _lane(
        latency={
            "bar_close_processing_ms": {"p95": 7_500, "n": 20},
            "decision_lag_ms": {"p95": 25, "n": 20},
        }
    )
    assert lane_bands(lane)["bar_close_lag"] == "degraded"
    chips = compute_chips({"lanes": [lane], "feed_health": {"candles": "ok"}})
    assert chips["DECISION"] == {"band": "degraded", "label": "bar close lag"}


def test_hard_bar_close_latency_blocks_decision_chip():
    lane = _lane(
        latency={
            "bar_close_processing_ms": {"p95": 16_000, "n": 20},
            "decision_lag_ms": {"p95": 25, "n": 20},
        }
    )
    chips = compute_chips({"lanes": [lane], "feed_health": {"candles": "ok"}})
    assert chips["DECISION"] == {"band": "blocked", "label": "bar close lag"}


def test_recovered_hard_tail_is_degraded_not_blocked():
    lane = _lane(
        latency={
            "bar_close_processing_ms": {
                "p95": 16_000,
                "n": 25,
                "recent": [9_000, 8_000, 7_000, 7_500, 6_500],
            },
            "decision_lag_ms": {"p95": 25, "n": 25, "recent": [25] * 5},
        },
        arm_blocked=None,
    )

    assert lane_bands(lane)["bar_close_lag"] == "degraded"
    chips = compute_chips({"lanes": [lane], "feed_health": {"candles": "ok"}})
    assert chips["DECISION"] == {"band": "degraded", "label": "bar close lag"}


def test_annotate_attaches_chips_and_per_lane_bands():
    snap = {"lanes": [_lane(drawdown_pct=7.35, dd_limit_pct=6.0)], "feed_health": {"candles": "ok"}}
    annotate(snap)
    assert "chips" in snap and snap["lanes"][0]["bands"]["dd"] == "blocked"


def test_latency_is_collecting_until_runtime_minimum_samples() -> None:
    lane = _lane(
        latency={
            "bar_close_processing_ms": {"p95": 2_500, "n": 2},
            "decision_lag_ms": {"p95": 25, "n": 2},
        }
    )
    assert lane_bands(lane)["bar_close_lag"] == "unknown"
    chips = compute_chips({"lanes": [lane], "feed_health": {"candles": "ok"}})
    assert chips["DECISION"] == {"band": "unknown", "label": "collecting 2/20"}
