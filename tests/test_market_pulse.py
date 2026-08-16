from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.market_pulse import (
    BRIEF_SCHEMA_VERSION,
    OBSERVATION_DISCLAIMER,
    MarketPulseService,
)
from vnedge.data.candles import Candle, CandleParquetStore
from vnedge.data.gaps import GapKind, GapParquetStore, GapRecord

D = Decimal
START = datetime(2026, 8, 15, tzinfo=UTC)


def candle(hour: int, *, volume: str = "10") -> Candle:
    opened = START + timedelta(hours=hour)
    price = D("100") + D(hour)
    return Candle(
        symbol="BTCUSDT",
        timeframe="1h",
        open_time=opened,
        close_time=opened + timedelta(hours=1),
        open=price,
        high=price + D("1"),
        low=price - D("1"),
        close=price + D("0.5"),
        volume=D(volume),
        quote_volume=D(volume) * (price + D("0.25")),
        trade_count=4,
        vwap=price + D("0.25"),
        is_closed=True,
    )


def service(tmp_path, *, analyzer=None) -> MarketPulseService:
    candle_store = CandleParquetStore(tmp_path / "candles", exchange="binanceusdm")
    candle_store.upsert(tuple(candle(hour, volume=str(hour + 1)) for hour in range(26)))
    kwargs = {"analyzer": analyzer} if analyzer is not None else {}
    return MarketPulseService(
        tmp_path / "candles",
        tmp_path / "gaps",
        tmp_path / "analysis.sqlite",
        **kwargs,
    )


def test_pulse_metrics_are_closed_hour_measurements_only(tmp_path) -> None:
    pulse = service(tmp_path).pulse(
        "binanceusdm",
        "BTCUSDT",
        limit=24,
        runtime={
            "data_degraded": False,
            "price": {"bid": 125.4, "ask": 125.6, "mid": 125.5},
            "pulse": {"forming": {"symbol": "BTCUSDT", "open": 125.5}},
        },
    )
    assert pulse["read_only"] is True
    assert pulse["can_trade"] is False
    assert pulse["live_orders_enabled"] is False
    assert len(pulse["hours"]) == 24
    assert pulse["hours"][-1]["forming"] is False
    assert pulse["hours"][-1]["volume_rank_24h"] == 1.0
    assert pulse["forming"]["open"] == 125.5
    assert pulse["forming"]["status"] == "awaiting_trades"
    assert pulse["forming"]["dual_avwap_bias"] == "n/a"
    assert pulse["forming"]["dual_avwap_reason"] == "no_confirmed_swing_pair"
    assert pulse["book"]["mid"] == 125.5
    assert pulse["market"]["last"] == 125.5
    assert pulse["market"]["mid"] == 125.5
    assert pulse["market"]["session_label"] in {
        "asia",
        "europe",
        "us_overlap",
        "us",
        "off_session",
    }
    assert pulse["indicators"]["avwap_unavailable_reason"] == (
        "no confirmed swing pair"
    )
    assert pulse["as_of_utc"] == pulse["as_of"]
    assert pulse["fee_wall_bps"] > 0
    assert len(pulse["session_vwap_series"]) == 24
    assert pulse["avwap_series"] is None
    assert pulse["hours"][-1]["open_time_utc"] == pulse["hours"][-1]["open_time"]
    assert pulse["hours"][-1]["is_gap"] is False


def test_single_symbol_runtime_quote_never_leaks_into_other_market_strip_row(
    tmp_path,
) -> None:
    payload = service(tmp_path).pulse(
        "binanceusdm",
        "BTCUSDT",
        runtime={
            "symbol": "ETH/USDT:USDT",
            "price": {"bid": 99.0, "ask": 101.0, "mid": 100.0},
            "feed_health": {"last_update_ms": 5.0},
        },
    )

    assert payload["book"] is None
    assert payload["market"]["mid"] is None
    assert payload["market"]["feed_age_ms"] is None


def test_pulse_dual_avwap_uses_only_latest_confirmed_swing_pair(tmp_path) -> None:
    lows = ["95", "94", "93", "85", "92", "93", "94", "95", "96", "97"]
    highs = ["115", "116", "117", "118", "119", "130", "120", "119", "118", "117"]
    rows = tuple(
        replace(candle(index), low=D(low), high=D(high))
        for index, (low, high) in enumerate(zip(lows, highs))
    )
    CandleParquetStore(tmp_path / "candles", exchange="binanceusdm").upsert(rows)
    pulse_service = MarketPulseService(
        tmp_path / "candles",
        tmp_path / "gaps",
        tmp_path / "analysis.sqlite",
        clock=lambda: rows[-1].close_time,
    )

    payload = pulse_service.pulse(
        "binanceusdm",
        "BTCUSDT",
        runtime={
            "symbol": "BTCUSDT",
            "price": {"bid": 110.9, "ask": 111.1, "mid": 111.0},
        },
    )

    assert payload["hours"][7]["dual_avwap_bias"] == "n/a"
    latest = payload["hours"][-1]
    assert latest["dual_avwap_bias"] == "strong_long"
    assert latest["avwap_low_anchor_utc"] == "2026-08-15T03:00:00Z"
    assert latest["avwap_high_anchor_utc"] == "2026-08-15T05:00:00Z"
    assert latest["avwap_low_confirmed_at_utc"] == "2026-08-15T07:00:00Z"
    assert latest["avwap_high_confirmed_at_utc"] == "2026-08-15T09:00:00Z"
    assert latest["avwap_low"] == pytest.approx(106.25)
    assert latest["avwap_high"] == pytest.approx(107.25)
    assert payload["forming"]["dual_avwap_bias"] == "strong_long"
    assert payload["forming"]["dual_avwap_reason"] is None
    assert len(payload["dual_avwap_series"]["low"]) == 4
    assert len(payload["dual_avwap_series"]["high"]) == 2


def test_known_gap_resets_dual_avwap_and_blocks_cross_gap_anchors(tmp_path) -> None:
    lows = ["95", "94", "93", "85", "92", "93", "94", "95", "96", "97"]
    highs = ["115", "116", "117", "118", "119", "130", "120", "119", "118", "117"]
    rows = tuple(
        replace(candle(index), low=D(low), high=D(high))
        for index, (low, high) in enumerate(zip(lows, highs))
    )
    CandleParquetStore(tmp_path / "candles", exchange="binanceusdm").upsert(rows)
    GapParquetStore(tmp_path / "gaps").upsert(
        (
            GapRecord(
                "BTCUSDT",
                "binanceusdm",
                GapKind.STREAM_STALE,
                rows[8].open_time,
                rows[8].close_time,
                rows[8].close_time,
                "coverage unknown inside swing window",
            ),
        )
    )
    pulse_service = MarketPulseService(
        tmp_path / "candles",
        tmp_path / "gaps",
        tmp_path / "analysis.sqlite",
        clock=lambda: rows[-1].close_time,
    )

    payload = pulse_service.pulse("binanceusdm", "BTCUSDT")

    assert payload["hours"][7]["avwap_low"] is not None
    assert payload["hours"][8]["data_quality"] == "gap"
    assert payload["hours"][8]["dual_avwap_bias"] == "n/a"
    assert payload["hours"][9]["dual_avwap_bias"] == "n/a"
    assert payload["hours"][9]["avwap_low_anchor_utc"] is None
    assert payload["hours"][9]["avwap_high_anchor_utc"] is None


def test_gap_quality_and_runtime_degradation_are_never_hidden(tmp_path) -> None:
    pulse_service = service(tmp_path)
    gap = GapRecord(
        "BTCUSDT",
        "binanceusdm",
        GapKind.STREAM_STALE,
        START + timedelta(hours=24, minutes=10),
        START + timedelta(hours=24, minutes=20),
        START + timedelta(hours=24, minutes=20),
        "websocket disconnected",
    )
    GapParquetStore(tmp_path / "gaps").upsert((gap,))
    payload = pulse_service.pulse(
        "binanceusdm",
        "BTCUSDT",
        runtime={"data_degraded": False},
    )
    assert payload["status"] == "degraded"
    assert payload["data_quality"] == "gap"
    affected = next(row for row in payload["hours"] if row["open_time"] == "2026-08-16T00:00:00Z")
    assert affected["data_quality"] == "gap"
    assert affected["is_gap"] is True
    assert any(alert["kind"] == "gap" for alert in payload["alerts"])


def test_stale_canonical_hour_is_degraded_not_live(tmp_path) -> None:
    pulse_service = service(tmp_path)
    pulse_service.clock = lambda: START + timedelta(hours=30)

    payload = pulse_service.pulse(
        "binanceusdm",
        "BTCUSDT",
        runtime={"data_degraded": False},
    )

    assert payload["status"] == "degraded"
    assert payload["data_quality"] == "degraded"
    assert payload["alerts"][0]["kind"] == "stale"


def test_analysis_uses_fixed_context_is_cached_and_cannot_grant_orders(tmp_path) -> None:
    calls: list[dict] = []

    def analyzer(context):
        calls.append(dict(context))
        bias = context["inputs"]["structure"]["dual_avwap_bias"]
        return {
            "state": {"label": "range", "summary": "Measured state."},
            "what_mattered": {
                "bullets": ["Range was measured.", "Volume rank was measured."]
            },
            "structure": {
                "summary": "Closed-hour structure from measured inputs.",
                "bias_tag": bias,
            },
            "risks": {"bullets": ["Persistence remains uncertain."]},
            "watch_next": {"summary": "Observe the next closed hour."},
        }

    pulse_service = service(tmp_path, analyzer=analyzer)
    opened = START + timedelta(hours=25)
    first = pulse_service.analysis("binanceusdm", "BTCUSDT", opened)
    second = pulse_service.analysis("binanceusdm", "BTCUSDT", opened)
    assert first == second
    assert len(calls) == 1
    assert set(calls[0]) == {"inputs"}
    assert set(calls[0]["inputs"]) == {
        "ohlc",
        "volume",
        "vwap",
        "structure",
        "quality",
        "context_hours",
    }
    assert len(calls[0]["inputs"]["context_hours"]) == 6
    assert first["schema_version"] == BRIEF_SCHEMA_VERSION
    assert first["hour_open_utc"] == "2026-08-16T01:00:00Z"
    assert first["hour_close_utc"] == "2026-08-16T02:00:00Z"
    assert first["disclaimer"] == OBSERVATION_DISCLAIMER


def test_unsafe_ai_output_falls_back_to_grounded_observation(tmp_path) -> None:
    def unsafe_analyzer(_context):
        return {
            "state": {"label": "expansion", "summary": "Guaranteed rally to 999999."},
            "what_mattered": {"bullets": ["Buy now.", "Use leverage."]},
            "structure": {"summary": "Go long.", "bias_tag": "unavailable"},
            "risks": {"bullets": ["Set a stop and target."]},
            "watch_next": {"summary": "Enter or exit next hour."},
        }

    pulse_service = service(tmp_path, analyzer=unsafe_analyzer)
    brief = pulse_service.analysis(
        "binanceusdm",
        "BTCUSDT",
        START + timedelta(hours=25),
    )
    stored = pulse_service.analysis_store.get(
        "binanceusdm", "BTCUSDT", "2026-08-16T01:00:00Z"
    )
    assert stored is not None
    text = json.dumps(stored.to_dict()).lower()
    assert re.search(
        r"\b(?:buy|sell|long|short|enter|exit|target|stop|leverage|guaranteed)\b",
        text,
    ) is None
    assert "999999" not in text
    assert brief["model"] == "deterministic-fallback-v1"
    assert brief["disclaimer"] == OBSERVATION_DISCLAIMER


def test_gap_brief_forces_degraded_state_and_server_flags(tmp_path) -> None:
    pulse_service = service(tmp_path)
    opened = START + timedelta(hours=24)
    GapParquetStore(tmp_path / "gaps").upsert(
        (
            GapRecord(
                "BTCUSDT",
                "binanceusdm",
                GapKind.STREAM_STALE,
                opened + timedelta(minutes=5),
                opened + timedelta(minutes=12),
                opened + timedelta(minutes=12),
                "seven unproven minutes",
            ),
        )
    )

    brief = pulse_service.analysis("binanceusdm", "BTCUSDT", opened)

    assert brief["data_quality"] == "gap"
    assert brief["inputs"]["quality"] == {
        "data_quality": "gap",
        "gap_minutes": 7.0,
        "stream_healthy": False,
    }
    assert brief["sections"]["state"]["label"] == "degraded_data"
    first_risk = brief["sections"]["risks"]["bullets"][0].lower()
    assert "feed" in first_risk or "gap" in first_risk
    assert brief["flags"]["feed_degraded"] is True


def test_analyzer_cannot_mutate_server_owned_gap_quality(tmp_path) -> None:
    def mutating_analyzer(context):
        context["inputs"]["quality"]["data_quality"] = "ok"
        return {
            "state": {"label": "range", "summary": "Measured state."},
            "what_mattered": {
                "bullets": ["Range was measured.", "Volume was measured."]
            },
            "structure": {
                "summary": "Measured structure.",
                "bias_tag": "unavailable",
            },
            "risks": {"bullets": ["Persistence remains uncertain."]},
            "watch_next": {"summary": "Observe the next closed hour."},
        }

    pulse_service = service(tmp_path, analyzer=mutating_analyzer)
    opened = START + timedelta(hours=24)
    GapParquetStore(tmp_path / "gaps").upsert(
        (
            GapRecord(
                "BTCUSDT",
                "binanceusdm",
                GapKind.STREAM_STALE,
                opened + timedelta(minutes=1),
                opened + timedelta(minutes=2),
                opened + timedelta(minutes=2),
                "mutation guard",
            ),
        )
    )

    brief = pulse_service.analysis("binanceusdm", "BTCUSDT", opened)

    assert brief["data_quality"] == "gap"
    assert brief["inputs"]["quality"]["data_quality"] == "gap"
    assert brief["flags"]["feed_degraded"] is True
    assert brief["sections"]["state"]["label"] == "degraded_data"


def test_new_gap_evidence_invalidates_cached_clean_brief(tmp_path) -> None:
    pulse_service = service(tmp_path)
    opened = START + timedelta(hours=24)
    clean = pulse_service.analysis("binanceusdm", "BTCUSDT", opened)
    assert clean["data_quality"] == "ok"

    GapParquetStore(tmp_path / "gaps").upsert(
        (
            GapRecord(
                "BTCUSDT",
                "binanceusdm",
                GapKind.STREAM_STALE,
                opened + timedelta(minutes=20),
                opened + timedelta(minutes=25),
                opened + timedelta(minutes=25),
                "late integrity evidence",
            ),
        )
    )
    replaced = pulse_service.analysis("binanceusdm", "BTCUSDT", opened)

    assert replaced["data_quality"] == "gap"
    assert replaced["inputs"] != clean["inputs"]
    assert replaced["sections"]["state"]["label"] == "degraded_data"


def test_model_failure_uses_schema_valid_text_only_fallback(tmp_path) -> None:
    def offline(_context):
        raise TimeoutError("model unavailable")

    brief = service(tmp_path, analyzer=offline).analysis(
        "binanceusdm", "BTCUSDT", START + timedelta(hours=25)
    )

    assert brief["model"] == "deterministic-fallback-v1"
    assert "Automated brief — model offline." in brief["sections"]["risks"]["bullets"]
    assert list(brief["sections"]) == [
        "state",
        "what_mattered",
        "structure",
        "risks",
        "watch_next",
    ]


def test_forming_or_unknown_hour_has_no_brief(tmp_path) -> None:
    pulse_service = service(tmp_path)
    with pytest.raises(KeyError, match="no closed 1h candle"):
        pulse_service.analysis(
            "binanceusdm", "BTCUSDT", START + timedelta(hours=26)
        )


def test_pulse_api_is_authenticated_and_analysis_is_cached(tmp_path) -> None:
    provider = SnapshotProvider()
    provider.publish({"data_degraded": False, "price": {"mid": 125.5}})
    app = create_app(provider, token="secret", market_pulse_service=service(tmp_path))
    client = TestClient(app)

    assert client.get("/api/pulse/BTCUSDT").status_code == 401
    pulse = client.get("/api/pulse/BTCUSDT?token=secret&n=12")
    assert pulse.status_code == 200
    assert len(pulse.json()["hours"]) == 12

    hours = client.get("/api/pulse/BTCUSDT/hours?token=secret&n=3")
    assert hours.status_code == 200
    assert hours.json()["count"] == 3

    opened = "2026-08-16T01:00:00Z"
    analysis = client.get(
        f"/api/pulse/BTCUSDT/hours/{opened}/analysis?token=secret"
    )
    assert analysis.status_code == 200
    assert analysis.json()["disclaimer"] == OBSERVATION_DISCLAIMER
    forming = client.get(
        "/api/pulse/BTCUSDT/hours/2026-08-16T02:00:00Z/analysis?token=secret"
    )
    assert forming.status_code == 404


def test_pulse_websocket_is_token_gated_and_coalesced(tmp_path) -> None:
    provider = SnapshotProvider()
    provider.publish({"data_degraded": False})
    client = TestClient(
        create_app(provider, token="secret", market_pulse_service=service(tmp_path))
    )
    with client.websocket_connect(
        "/api/pulse/stream?token=secret&symbol=BTCUSDT&exchange=binanceusdm"
    ) as websocket:
        payload = websocket.receive_json()
    assert payload["symbol"] == "BTCUSDT"
    assert payload["can_trade"] is False
