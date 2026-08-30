import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from vnedge.research.quote_parity_monitor import (
    QuoteParityPolicy,
    _mechanism_lifecycle_guard,
    build_quote_parity_report,
    classify_parity,
    latest_runtime_start,
    load_quote_capture,
)
from vnedge.runtime.multi_lane import LaneSpec


def _eligible_replay(*, quotes: int = 2_000) -> dict:
    return {
        "quotes_used": quotes,
        "capture_quality": {
            "mode": "lane_consumed",
            "parity_eligible": True,
        },
    }


def test_zero_intent_exact_match_is_collecting_not_cutover_proof():
    status, reasons = classify_parity(
        _eligible_replay(),
        {
            "mechanism_exact_parity": True,
            "approval_parity_eligible": True,
            "matched_intents": 0,
        },
        capture_duration=timedelta(hours=3),
        policy=QuoteParityPolicy(),
    )

    assert status == "collecting"
    assert reasons == ("matched_intents_below_minimum",)


def test_nontrivial_exact_match_passes_only_after_duration_and_quote_floor():
    policy = QuoteParityPolicy(
        min_duration=timedelta(hours=2), min_quotes=1_000, min_matched_intents=2
    )

    status, reasons = classify_parity(
        _eligible_replay(quotes=10_000),
        {
            "mechanism_exact_parity": True,
            "approval_parity_eligible": True,
            "matched_intents": 2,
        },
        capture_duration=timedelta(hours=2, minutes=1),
        policy=policy,
    )

    assert status == "passed"
    assert reasons == ()


def test_mismatch_fails_before_sample_size_is_considered():
    status, reasons = classify_parity(
        _eligible_replay(quotes=1),
        {
            "mechanism_exact_parity": False,
            "approval_parity_eligible": False,
            "matched_intents": 0,
        },
        capture_duration=timedelta(seconds=1),
        policy=QuoteParityPolicy(),
    )

    assert status == "mismatch"
    assert reasons == ("live_replay_mismatch",)


def test_mechanism_only_is_not_mislabeled_as_live_replay_mismatch():
    status, reasons = classify_parity(
        _eligible_replay(quotes=10_000),
        {
            "mechanism_exact_parity": True,
            "approval_parity_eligible": False,
            "matched_intents": 2,
        },
        capture_duration=timedelta(hours=3),
        policy=QuoteParityPolicy(),
    )

    assert status == "mechanism_only"
    assert reasons == ("approval_parity_disabled",)


def test_recorded_lifecycle_guard_reuses_rejection_without_claiming_gateway_parity():
    guard = _mechanism_lifecycle_guard(
        [
            {
                "kind": "shadow_intent",
                "payload": {
                    "intent_key": "session_continuation_realtime_v2|BTCUSDT|long|1",
                    "strategy_id": "session_continuation_realtime_v2",
                    "symbol": "BTC/USDT:USDT",
                    "approved": False,
                    "failed_checks": ["candle_path:tm_age_hard"],
                    "intent": {"side": "long", "notional_usd": 3000.0},
                    "quote_event_ts": "2026-08-29T13:46:01Z",
                    "episode_id": 7,
                    "margin_usd": 100.0,
                },
            }
        ],
        strategy_id="session_continuation_realtime_v2",
        symbol="BTCUSDT",
    )

    class Fire:
        side = "long"
        episode_id = 7

    approval = guard(Fire(), 10, datetime(2026, 8, 29, 13, 46, 1, tzinfo=UTC))

    assert approval.approved is False
    assert approval.intent_key.endswith("|1")
    assert approval.failed_checks == ("candle_path:tm_age_hard",)


def test_latest_runtime_start_is_bound_to_exact_lane_identity():
    spec = LaneSpec(
        lane_id="lane",
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        timeframe="15m",
        strategy_id="range_expansion_realtime_v1",
    )
    records = [
        {
            "kind": "paper_lane_heartbeat",
            "payload": {
                "reason": "runner_started",
                "started_at": "2026-08-29T01:00:00Z",
                "strategy_id": spec.strategy_id,
                "symbol": "ETH/USDT:USDT",
                "timeframe": "15m",
            },
        },
        {
            "kind": "paper_lane_heartbeat",
            "payload": {
                "reason": "runner_started",
                "started_at": "2026-08-29T02:00:00Z",
                "strategy_id": spec.strategy_id,
                "symbol": "BTCUSDT",
                "timeframe": "15m",
            },
        },
    ]

    assert latest_runtime_start(records, spec) == datetime(2026, 8, 29, 2, tzinfo=UTC)


def test_empty_post_restart_quote_window_is_collecting_not_error(tmp_path: Path):
    runtime_start = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)
    spec = LaneSpec(
        lane_id="session-lane",
        exchange="binanceusdm",
        symbol="BTC/USDT:USDT",
        timeframe="15m",
        strategy_id="session_continuation_realtime_v2",
    )
    journal_root = tmp_path / "journals"
    journal_root.mkdir()
    (journal_root / f"{spec.lane_id}.journal.jsonl").write_text(
        json.dumps(
            {
                "ts": runtime_start.isoformat(),
                "kind": "paper_lane_heartbeat",
                "payload": {
                    "reason": "runner_started",
                    "started_at": runtime_start.isoformat(),
                    "strategy_id": spec.strategy_id,
                    "symbol": spec.symbol,
                    "timeframe": spec.timeframe,
                },
            }
        )
        + "\n"
    )
    lane_day = (
        tmp_path
        / "quotes"
        / f"lane={spec.lane_id}"
        / "exchange=binanceusdm"
        / "symbol=BTCUSDT"
        / "20260830"
    )
    lane_day.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "ts_ms": int((runtime_start - timedelta(seconds=1)).timestamp() * 1000),
                "captured_at_ms": 1,
                "capture_overflow_drops": 0,
                "lane_id": spec.lane_id,
                "exchange": spec.exchange,
                "symbol": spec.data_symbol,
            }
        ]
    ).to_parquet(lane_day / "quotes.parquet", index=False)

    report = build_quote_parity_report(
        [spec],
        candle_root=tmp_path / "candles",
        quote_root=tmp_path / "quotes",
        journal_root=journal_root,
        policy=QuoteParityPolicy(min_duration=timedelta(hours=2)),
        now=runtime_start + timedelta(minutes=1),
    )

    assert report["summary"]["statuses"] == {"collecting": 1}
    assert report["lanes"][0]["reasons"] == [
        "approval_parity_disabled",
        "capture_duration_below_minimum",
        "quote_count_below_minimum",
        "matched_intents_below_minimum",
    ]


def test_quote_capture_loader_excludes_previous_runner_rows(tmp_path: Path):
    lane = tmp_path / "lane=x" / "exchange=binanceusdm" / "symbol=BTCUSDT"
    day = lane / "20260829"
    day.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "ts_ms": 1_787_964_000_000,
                "captured_at_ms": 1,
                "capture_overflow_drops": 0,
                "lane_id": "x",
                "exchange": "binanceusdm",
                "symbol": "BTCUSDT",
            },
            {
                "ts_ms": 1_787_967_600_000,
                "captured_at_ms": 2,
                "capture_overflow_drops": 0,
                "lane_id": "x",
                "exchange": "binanceusdm",
                "symbol": "BTCUSDT",
            },
            {
                "ts_ms": 1_787_967_601_000,
                "captured_at_ms": 3,
                "capture_overflow_drops": 0,
                "lane_id": "x",
                "exchange": "binanceusdm",
                "symbol": "BTCUSDT",
            },
        ]
    ).to_parquet(day / "quotes.parquet", index=False)
    runtime_start = datetime.fromtimestamp(1_787_967_600, tz=UTC)

    loaded = load_quote_capture(
        lane,
        runtime_start=runtime_start,
        now=runtime_start + timedelta(seconds=2),
    )

    assert loaded["captured_at_ms"].tolist() == [2, 3]
