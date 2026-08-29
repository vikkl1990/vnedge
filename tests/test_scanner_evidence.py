import json
from pathlib import Path

import pandas as pd
import pytest

from vnedge.research import scanner_evidence
from vnedge.research.scanner_evidence import (
    apply_contiguous_warmup_quality,
    build_daily_report,
    normalize_canonical_candles,
    read_lane_evals,
)
from vnedge.runtime.squeeze_observe import ScannerApproval
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.realtime_entry import RealtimeEntryArm
from vnedge.strategy.scanner_contracts import ScannerRuntimeContract
from vnedge.strategy.squeeze_expansion_breakout_v3 import SqueezeExpansionV3Params


def test_canonical_storage_normalization_restores_live_closed_quality_contract():
    stored = pd.DataFrame(
        {
            "open_time": [pd.Timestamp("2026-01-01T00:00:00Z")],
            "open": ["100.0"],
            "high": ["101.0"],
            "low": ["99.0"],
            "close": ["100.5"],
            "volume": ["2.0"],
            "quote_volume": ["200.5"],
            "trade_count": [3],
        }
    )

    normalized = normalize_canonical_candles(stored)

    assert normalized.loc[0, "timestamp"] == pd.Timestamp("2026-01-01T00:00:00Z")
    assert bool(normalized.loc[0, "is_closed"]) is True
    assert normalized.loc[0, "data_quality"] == "ok"
    assert normalized.loc[0, "candle_source"] == "canonical_tick_lake"


def test_runtime_frame_normalization_preserves_explicit_non_ok_state():
    runtime = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-01-01T00:00:00Z")],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [2.0],
            "data_quality": ["gap"],
            "is_closed": [False],
            "candle_source": ["exchange_ohlcv"],
        }
    )

    normalized = normalize_canonical_candles(runtime)

    assert normalized.loc[0, "data_quality"] == "gap"
    assert bool(normalized.loc[0, "is_closed"]) is False
    assert normalized.loc[0, "candle_source"] == "exchange_ohlcv"


def test_skipped_empty_bucket_does_not_contaminate_warmup():
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:15:00Z",
                    "2026-01-01T00:45:00Z",
                    "2026-01-01T01:00:00Z",
                    "2026-01-01T01:15:00Z",
                    "2026-01-01T01:30:00Z",
                ]
            ),
            "data_quality": ["ok"] * 6,
        }
    )

    qualified, breaks = apply_contiguous_warmup_quality(
        frame, timeframe_seconds=900, warmup_bars=2
    )

    assert breaks == 0
    assert qualified["data_quality"].tolist() == ["ok"] * 6


def test_explicit_gap_contaminates_exactly_the_causal_warmup_horizon():
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2026-01-01", periods=6, freq="15min", tz="UTC"
            ),
            "data_quality": ["ok", "ok", "gap", "ok", "ok", "ok"],
        }
    )

    qualified, breaks = apply_contiguous_warmup_quality(
        frame, timeframe_seconds=900, warmup_bars=2
    )

    assert breaks == 1
    assert qualified["data_quality"].tolist() == [
        "ok",
        "ok",
        "gap",
        "gap",
        "gap",
        "ok",
    ]


def test_daily_report_groups_exact_strategy_and_failed_gates(tmp_path: Path):
    journal = tmp_path / "lane.jsonl"
    records = [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "kind": "lane_eval",
            "payload": {
                "strategy_id": "avwap_reclaim_15m_v1",
                "fired": False,
                "eligible": False,
                "all_failed_gates": ["avwap_not_reclaimed"],
                "features": {},
                "distance_to_threshold": {"distance": 2.0},
                "backfill": True,
            },
        },
        {
            "ts": "2026-01-01T00:15:00+00:00",
            "kind": "lane_eval",
            "payload": {
                "strategy_id": "avwap_reclaim_15m_v1",
                "fired": True,
                "eligible": True,
                "all_failed_gates": [],
                "features": {},
                "distance_to_threshold": {},
            },
        },
    ]
    journal.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    report = build_daily_report(read_lane_evals([journal]))
    assert report["evaluations"] == 2
    assert report["fires"] == 1
    row = report["strategies"][0]
    assert row["failed_gates"] == {"avwap_not_reclaimed": 1}
    assert row["capital_eligible"] is False


def test_replay_uses_single_book_next_open_and_dual_costs(monkeypatch):
    class _TwoSignals(BaseStrategy):
        strategy_id = "test_two_signals"
        warmup_bars = 0

        def prepare(self, candles):
            return candles.copy()

        def signal(self, df, index):
            del df
            if index in {0, 1}:
                return SignalIntent(
                    side="long", stop_price=90.0, take_profit_price=110.0,
                    reason="test",
                )
            return None

    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=4, freq="15min", tz="UTC"),
            "open": [99.0, 100.0, 101.0, 100.0],
            "high": [100.0, 102.0, 111.0, 101.0],
            "low": [98.0, 99.0, 100.0, 99.0],
            "close": [99.5, 101.0, 110.0, 100.0],
            "volume": [1.0] * 4,
        }
    )
    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _TwoSignals)

    result = scanner_evidence.replay_scanner("test_two_signals", candles)

    assert result["signals"] == 2
    assert result["trades"] == 1
    assert result["position_conflicts"] == 1
    outcome = next(row["outcome"] for row in result["records"] if row.get("admitted"))
    assert outcome["entry"] == 100.0
    assert outcome["exit"] == 110.0
    assert result["schema_version"] == 2
    assert result["net_bps_semantics"] == "booked_execution"
    assert outcome["net_bps"] == outcome["net_execution_bps"]
    assert outcome["cost_bps"] == outcome["execution_cost_bps"]
    assert outcome["funding_bps"] == 0.0
    assert outcome["net_execution_bps"] > outcome["net_gate_bps"]


def test_closed_replay_uses_venue_contract_cost_not_private_strategy_cost(monkeypatch):
    class _PrivateCostStrategy(BaseStrategy):
        strategy_id = "private_cost"
        warmup_bars = 0

        class params:
            round_trip_cost_bps = 1.0

        def prepare(self, candles):
            return candles.copy()

        def signal(self, df, index):
            if index != 0:
                return None
            return SignalIntent(
                side="long", stop_price=90.0, take_profit_price=110.0, reason="test"
            )

    contract = ScannerRuntimeContract(
        strategy_id="private_cost",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=2,
        rationale="test",
    )
    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _PrivateCostStrategy)
    monkeypatch.setattr(scanner_evidence, "scanner_runtime_contract", lambda _: contract)
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC"),
            "open": [100.0, 100.0, 100.0],
            "high": [101.0, 111.0, 101.0],
            "low": [99.0, 99.0, 99.0],
            "close": [100.0, 110.0, 100.0],
            "volume": [1.0, 1.0, 1.0],
        }
    )

    result = scanner_evidence.replay_scanner(
        "private_cost", candles, exchange_id="delta_india"
    )

    assert result["cost_profile"] == "delta_swing"
    assert result["execution_cost_bps"] == pytest.approx(15.8)
    assert result["gate_cost_bps"] == pytest.approx(18.8)
    outcome = next(row["outcome"] for row in result["records"] if row.get("admitted"))
    assert outcome["execution_cost_bps"] == pytest.approx(15.8)
    assert outcome["gate_cost_bps"] == pytest.approx(18.8)
    assert outcome["cost_bps"] == pytest.approx(15.8)
    assert outcome["net_bps"] == outcome["net_execution_bps"]
    assert outcome["net_gate_bps"] == pytest.approx(
        outcome["gross_bps"] - outcome["gate_cost_bps"]
    )
    assert outcome["net_gate_bps"] <= outcome["net_execution_bps"]
    assert result["net_bps"] == result["net_execution_bps"]
    assert result["funding_bps"] == 0.0
    assert result["performance_eligible"] is False


def test_closed_replay_refuses_quote_driven_scanner_without_bbo(monkeypatch):
    class _QuoteOnly(BaseStrategy):
        strategy_id = "quote_only"

        def prepare(self, candles):
            return candles.copy()

        def signal(self, df, index):
            del df, index

    contract = ScannerRuntimeContract(
        strategy_id="quote_only",
        timeframe="15m",
        cost_family="swing",
        max_holding_bars=2,
        rationale="test",
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
    )
    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _QuoteOnly)
    monkeypatch.setattr(scanner_evidence, "scanner_runtime_contract", lambda _: contract)
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="15min", tz="UTC"),
            "open": [100.0, 100.0],
            "high": [101.0, 101.0],
            "low": [99.0, 99.0],
            "close": [100.0, 100.0],
            "volume": [1.0, 1.0],
        }
    )

    with pytest.raises(ValueError, match="quote-driven.*lane-consumed BBO"):
        scanner_evidence.replay_scanner("quote_only", candles)


def test_replay_requires_and_binds_declared_canonical_context(monkeypatch):
    class _ContextStrategy(BaseStrategy):
        strategy_id = "test_context"
        warmup_bars = 0
        canonical_context_timeframes = ("4h",)

        def __init__(self):
            self.context = None

        def bind_canonical_context(self, timeframe, candles):
            assert timeframe == "4h"
            self.context = candles

        def prepare(self, candles):
            assert self.context is not None
            return candles.copy()

        def signal(self, df, index):
            del df, index

    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC"),
            "open": [100.0] * 3,
            "high": [101.0] * 3,
            "low": [99.0] * 3,
            "close": [100.0] * 3,
            "volume": [1.0] * 3,
        }
    )
    context = candles.iloc[:1].copy()
    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _ContextStrategy)

    with pytest.raises(ValueError, match="requires canonical context.*4h"):
        scanner_evidence.replay_scanner("test_context", candles)

    result = scanner_evidence.replay_scanner(
        "test_context", candles, context_candles={"4h": context}
    )
    assert result["canonical_context_timeframes"] == ["4h"]
    assert result["context_quality"]["4h"] == {
        "bars": 1,
        "continuity_breaks": 0,
        "quarantined_bars": 0,
    }
    assert result["setup_funnel"]["evaluations"] == 2
    assert result["setup_funnel"]["engine_status"] == "ready"
    assert result["setup_funnel"]["evaluable_bars"] == 2


def test_replay_gap_through_stop_never_gets_fictional_stop_fill(monkeypatch):
    class _GapSignal(BaseStrategy):
        strategy_id = "test_gap_signal"
        warmup_bars = 0

        def prepare(self, candles):
            return candles.copy()

        def signal(self, df, index):
            del df
            return (
                SignalIntent("long", stop_price=90.0, take_profit_price=110.0)
                if index == 0 else None
            )

    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC"),
            "open": [100.0, 85.0, 86.0],
            "high": [101.0, 88.0, 87.0],
            "low": [99.0, 84.0, 85.0],
            "close": [100.0, 86.0, 86.0],
            "volume": [1.0] * 3,
        }
    )
    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _GapSignal)

    result = scanner_evidence.replay_scanner("test_gap_signal", candles)
    outcome = next(row["outcome"] for row in result["records"] if row.get("admitted"))

    assert outcome["entry"] == 85.0
    assert outcome["exit"] == 85.0
    assert outcome["gross_bps"] == 0.0


def test_quote_replay_uses_runtime_engine_and_candle_before_quote_tie_break(monkeypatch):
    class _QuoteStrategy(BaseStrategy):
        strategy_id = "test_quote_scanner"
        warmup_bars = 0
        acceptance_params = SqueezeExpansionV3Params(
            acceptance_hold_seconds=0.5,
            min_acceptance_samples=2,
            break_buffer_bps=0.0,
        )

        def prepare(self, candles):
            frame = candles.copy()
            frame["rt_atr"] = 1.0
            return frame

        def signal(self, df, index):
            del df, index

        def realtime_arm(self, df, index):
            del df
            return RealtimeEntryArm(
                episode_id=index + 1,
                bar_index=index,
                long_level=101.0,
                short_level=99.0,
                atr=1.0,
                reference_price=100.0,
                allow_long=True,
                allow_short=False,
                expires_after_bars=2,
                reason=self.strategy_id,
            )

    contract = ScannerRuntimeContract(
        strategy_id="test_quote_scanner",
        timeframe="5m",
        cost_family="scalp",
        max_holding_bars=4,
        rationale="test",
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
    )
    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _QuoteStrategy)
    monkeypatch.setattr(scanner_evidence, "scanner_runtime_contract", lambda _: contract)
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 101.1],
            "high": [100.5, 101.3],
            "low": [99.5, 100.8],
            "close": [100.0, 101.15],
            "volume": [10.0, 12.0],
        }
    )
    boundary_ms = int(pd.Timestamp("2026-01-01T00:05:00Z").timestamp() * 1000)
    quotes = pd.DataFrame(
        {
            "ts_ms": [boundary_ms - 1, boundary_ms, boundary_ms + 600],
            "bid": [0.0, 101.09, 101.10],
            "ask": [101.1, 101.10, 101.11],
            "sequence": [0, 1, 2],
        }
    )

    result = scanner_evidence.replay_quote_scanner(
        "test_quote_scanner", candles, quotes
    )

    assert result["quotes_in"] == 3
    assert result["quotes_dropped"] == 1
    assert result["quotes_used"] == 2
    assert result["quotes_outside_window"] == 0
    assert result["capture_quality"]["mode"] == "external_book"
    assert result["capture_quality"]["parity_eligible"] is False
    assert result["intents"] == 1
    assert result["intent_keys"] == [
        "test_quote_scanner|BTCUSDT|long|1767225900600"
    ]
    intent = next(
        row["payload"] for row in result["records"] if row["kind"] == "shadow_intent"
    )
    assert intent["entry_price"] == 101.11
    assert intent["quote_sequence"] == 2


def test_quote_replay_runtime_start_rebuilds_same_bounded_episode_clock(monkeypatch):
    class _BoundedStrategy(BaseStrategy):
        strategy_id = "test_bounded_quote"
        warmup_bars = 2

        def prepare(self, candles):
            frame = candles.copy()
            frame["episode"] = range(1, len(frame) + 1)
            return frame

        def signal(self, df, index):
            del df, index

        def realtime_arm(self, df, index):
            return RealtimeEntryArm(
                episode_id=int(df.iloc[index]["episode"]),
                bar_index=index,
                long_level=101.0,
                short_level=99.0,
                atr=1.0,
                reference_price=100.0,
                allow_long=True,
                allow_short=False,
                expires_after_bars=2,
                reason=self.strategy_id,
            )

    contract = ScannerRuntimeContract(
        strategy_id="test_bounded_quote",
        timeframe="5m",
        cost_family="scalp",
        max_holding_bars=4,
        rationale="test",
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
    )
    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _BoundedStrategy)
    monkeypatch.setattr(scanner_evidence, "scanner_runtime_contract", lambda _: contract)
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=8, freq="5min", tz="UTC"),
            "open": [100.0] * 8,
            "high": [100.5] * 8,
            "low": [99.5] * 8,
            "close": [100.0] * 8,
            "volume": [10.0] * 8,
        }
    )
    quotes = pd.DataFrame(
        {
            "ts_ms": [int(pd.Timestamp("2026-01-01T00:25:01Z").timestamp() * 1000)],
            "bid": [100.0],
            "ask": [100.01],
        }
    )

    result = scanner_evidence.replay_quote_scanner(
        "test_bounded_quote",
        candles,
        quotes,
        runtime_start=pd.Timestamp("2026-01-01T00:20:30Z").to_pydatetime(),
    )

    # Seed is warmup+1 rows (00:05, 00:10, 00:15); the 00:20 close then has
    # the same process-local episode ordinal as a live runner started at 00:20:30.
    transition = next(
        r
        for r in result["records"]
        if r["kind"] == "scanner_transition"
        and r["payload"].get("source") == "canonical_close"
        and r["payload"].get("episode_id") == 4
    )
    assert transition["payload"]["episode_id"] == 4


def test_live_parity_keeps_rejected_intent_with_empty_order_payload():
    replay = {
        "strategy_id": "quote_v1",
        "symbol": "BTC/USDT:USDT",
        "source_window": {
            "start": "2026-01-01T00:00:00+00:00",
            "end_exclusive": "2026-01-01T01:00:00+00:00",
        },
        "records": [],
    }
    live = [
        {
            "kind": "shadow_intent",
            "payload": {
                "intent_key": "quote_v1|BTC/USDT:USDT|short|1",
                "approved": False,
                "intent": {},
                "quote_event_ts": "2026-01-01T00:30:00+00:00",
            },
        }
    ]

    result = scanner_evidence.compare_quote_replay_to_live(replay, live)

    assert result["live_intents"] == 1
    assert result["live_only"] == ["quote_v1|BTCUSDT|short|1"]


def test_quote_replay_live_parity_reports_keys_payloads_and_window():
    replay = {
        "strategy_id": "quote_v1",
        "symbol": "BTC/USDT:USDT",
        "source_window": {
            "start": "2026-01-01T00:00:00+00:00",
            "end_exclusive": "2026-01-01T01:00:00+00:00",
        },
        "records": [
            {
                "kind": "shadow_intent",
                "payload": {
                    "intent_key": "k1",
                    "approved": True,
                    "intent": {
                        "strategy_id": "quote_v1",
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                    },
                    "entry_price": 101.0,
                    "stop_price": 99.0,
                    "quote_sequence": 7,
                    "episode_id": 3,
                    "quote_event_ts": "2026-01-01T00:30:00+00:00",
                },
            }
        ],
        "approval_parity_eligible": True,
    }
    live = [
        replay["records"][0],
        {
            "kind": "shadow_intent",
            "payload": {
                "intent_key": "outside",
                "approved": True,
                "intent": {
                    "strategy_id": "quote_v1",
                    "symbol": "BTC/USDT:USDT",
                    "side": "long",
                },
                "quote_event_ts": "2026-01-01T02:00:00+00:00",
            },
        },
    ]

    exact = scanner_evidence.compare_quote_replay_to_live(replay, live)

    assert exact["exact_parity"] is True
    assert exact["matched_intents"] == 1
    changed = json.loads(json.dumps(live))
    changed[0]["payload"]["stop_price"] = 98.0
    mismatch = scanner_evidence.compare_quote_replay_to_live(replay, changed)
    assert mismatch["exact_parity"] is False
    assert mismatch["payload_mismatches"][0]["intent_key"] == "k1"


def test_journal_report_joins_intent_and_outcome(tmp_path: Path):
    journal = tmp_path / "lane.journal.jsonl"
    records = [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "kind": "shadow_intent",
            "payload": {
                "intent_key": "k1", "approved": True,
                "intent": {
                    "strategy_id": "session_continuation_15m_v1",
                    "side": "long", "quantity": 2.0, "notional_usd": 200.0,
                },
            },
        },
        {
            "ts": "2026-01-01T01:00:00+00:00",
            "kind": "shadow_outcome",
            "payload": {
                "intent_key": "k1", "side": "long", "entry_price": 100.0,
                "exit_price": 105.0, "fees_usd": 0.3,
                "virtual_net_usd": 9.7,
            },
        },
    ]
    journal.write_text("\n".join(json.dumps(row) for row in records) + "\n")

    report = scanner_evidence.build_journal_report([journal])

    row = report["strategies"][0]
    assert report["schema_version"] == 2
    assert row["virtual_approved"] == 1
    assert row["accepted_entries"] == 1
    assert report["accepted_entries"] == 1
    assert row["virtual_resolved"] == 1
    assert row["virtual_pending"] == 0
    assert row["gross_usd"] == 10.0
    assert row["net_execution_usd"] == 9.7
    assert row["observed_shadow_net_usd"] == 9.7
    assert report["net_execution_usd"] == 9.7
    assert report["performance_eligible"] is False


def test_journal_report_reads_only_a_bounded_recent_tail(tmp_path: Path):
    journal = tmp_path / "bounded.journal.jsonl"
    old = {
        "ts": "2026-01-01T00:00:00+00:00",
        "kind": "lane_eval",
        "payload": {"strategy_id": "old_v1", "fired": False},
    }
    recent = {
        "ts": "2026-01-02T00:00:00+00:00",
        "kind": "lane_eval",
        "payload": {"strategy_id": "recent_v1", "fired": True},
    }
    journal.write_text(
        (json.dumps(old) + "\n") * 100 + json.dumps(recent) + "\n",
        encoding="utf-8",
    )

    report = scanner_evidence.build_journal_report(
        [journal], max_bytes_per_journal=256, max_total_bytes=256
    )

    rows = {row["strategy_id"]: row for row in report["strategies"]}
    assert "recent_v1" in rows
    assert rows.get("old_v1", {}).get("evaluations", 0) < 100
    assert report["source_window"]["effective_bytes_per_journal"] == 256
    assert report["source_window"]["complete"] is False
    assert "bounded_journal_tail" in report["performance_blockers"]
    assert report["fires"] == 1


def _quote_strategy_class():
    class _QuoteStrategy(BaseStrategy):
        strategy_id = "test_quote_scanner"
        warmup_bars = 0
        acceptance_params = SqueezeExpansionV3Params(
            acceptance_hold_seconds=0.5,
            min_acceptance_samples=2,
            break_buffer_bps=0.0,
        )

        def prepare(self, candles):
            frame = candles.copy()
            frame["rt_atr"] = 1.0
            return frame

        def signal(self, df, index):
            del df, index

        def realtime_arm(self, df, index):
            del df
            return RealtimeEntryArm(
                episode_id=index + 1,
                bar_index=index,
                long_level=101.0,
                short_level=99.0,
                atr=1.0,
                reference_price=100.0,
                allow_long=True,
                allow_short=False,
                expires_after_bars=2,
                reason=self.strategy_id,
            )

    return _QuoteStrategy


def _quote_contract():
    return ScannerRuntimeContract(
        strategy_id="test_quote_scanner",
        timeframe="5m",
        cost_family="scalp",
        max_holding_bars=4,
        rationale="test",
        decision_engine="quote_acceptance_v2",
        exit_engine="scanner_exit_v1",
    )


def test_quote_replay_normalizes_canonical_open_time_and_decimal_frames(monkeypatch):
    """Canonical lake frames (open_time + Decimal columns) replay unchanged."""
    from decimal import Decimal

    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _quote_strategy_class())
    monkeypatch.setattr(scanner_evidence, "scanner_runtime_contract", lambda _: _quote_contract())
    candles = pd.DataFrame(
        {
            "open_time": pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [Decimal("100.0"), Decimal("101.1")],
            "high": [Decimal("100.5"), Decimal("101.3")],
            "low": [Decimal("99.5"), Decimal("100.8")],
            "close": [Decimal("100.0"), Decimal("101.15")],
            "volume": [Decimal("10.0"), Decimal("12.0")],
        }
    )
    boundary_ms = int(pd.Timestamp("2026-01-01T00:05:00Z").timestamp() * 1000)
    quotes = pd.DataFrame(
        {
            "ts_ms": [boundary_ms, boundary_ms + 600],
            "bid": [101.09, 101.10],
            "ask": [101.10, 101.11],
            "sequence": [1, 2],
        }
    )

    result = scanner_evidence.replay_quote_scanner("test_quote_scanner", candles, quotes)

    assert result["intents"] == 1
    assert result["intent_keys"] == [
        "test_quote_scanner|BTCUSDT|long|1767225900600"
    ]
    assert result["evidence_window"]["basis"] == "first_clean_quote"
    assert result["evidence_window"]["start"] == "2026-01-01T00:05:00+00:00"


def test_quote_replay_clamps_explicit_evidence_bounds_to_causal_window(monkeypatch):
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _quote_strategy_class())
    monkeypatch.setattr(scanner_evidence, "scanner_runtime_contract", lambda _: _quote_contract())
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 101.1],
            "high": [100.5, 101.3],
            "low": [99.5, 100.8],
            "close": [100.0, 101.15],
            "volume": [10.0, 12.0],
        }
    )
    boundary_ms = int(pd.Timestamp("2026-01-01T00:05:00Z").timestamp() * 1000)
    quotes = pd.DataFrame(
        {"ts_ms": [boundary_ms], "bid": [101.09], "ask": [101.10], "sequence": [1]}
    )

    result = scanner_evidence.replay_quote_scanner(
        "test_quote_scanner",
        candles,
        quotes,
        evidence_start=_datetime(2025, 12, 31, tzinfo=_UTC),
        evidence_end=_datetime(2026, 1, 2, tzinfo=_UTC),
    )

    assert result["evidence_window"]["basis"] == "explicit"
    # Clamped to the causal source window on both sides.
    assert result["evidence_window"]["start"] == result["source_window"]["start"]
    assert (
        result["evidence_window"]["end_exclusive"]
        == result["source_window"]["end_exclusive"]
    )


def test_quote_replay_refuses_incomplete_lane_capture(monkeypatch):
    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _quote_strategy_class())
    monkeypatch.setattr(scanner_evidence, "scanner_runtime_contract", lambda _: _quote_contract())
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 101.1],
            "high": [100.5, 101.3],
            "low": [99.5, 100.8],
            "close": [100.0, 101.15],
            "volume": [10.0, 12.0],
        }
    )
    quotes = pd.DataFrame(
        {
            "ts_ms": [int(pd.Timestamp("2026-01-01T00:05:00Z").timestamp() * 1000)],
            "bid": [101.09],
            "ask": [101.10],
            "capture_overflow_drops": [1],
        }
    )

    with pytest.raises(ValueError, match="capture overflowed"):
        scanner_evidence.replay_quote_scanner(
            "test_quote_scanner", candles, quotes
        )


def test_live_parity_audits_evidence_window_not_warmup_history():
    """A live intent during warm-up (before BBO coverage) must not fail parity."""
    replay = {
        "strategy_id": "quote_v1",
        "symbol": "BTC/USDT:USDT",
        "source_window": {
            "start": "2026-01-01T00:00:00+00:00",
            "end_exclusive": "2026-01-01T01:00:00+00:00",
        },
        "evidence_window": {
            "start": "2026-01-01T00:30:00+00:00",
            "end_exclusive": "2026-01-01T01:00:00+00:00",
            "basis": "first_clean_quote",
        },
        "records": [
            {
                "kind": "shadow_intent",
                "payload": {
                    "intent_key": "k1",
                    "approved": True,
                    "intent": {
                        "strategy_id": "quote_v1",
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                    },
                    "quote_event_ts": "2026-01-01T00:45:00+00:00",
                },
            }
        ],
        "approval_parity_eligible": True,
    }
    live = [
        replay["records"][0],
        {
            "kind": "shadow_intent",
            "payload": {
                "intent_key": "warmup_only_live",
                "approved": True,
                "intent": {
                    "strategy_id": "quote_v1",
                    "symbol": "BTC/USDT:USDT",
                    "side": "long",
                },
                "quote_event_ts": "2026-01-01T00:10:00+00:00",
            },
        },
    ]

    result = scanner_evidence.compare_quote_replay_to_live(replay, live)

    assert result["exact_parity"] is True
    assert result["live_only"] == []
    assert result["evidence_window"]["start"] == "2026-01-01T00:30:00+00:00"


def test_live_parity_normalizes_historical_native_symbol_intent_keys():
    replay_record = {
        "kind": "shadow_intent",
        "payload": {
            "intent_key": "quote_v1|BTCUSDT|long|1000",
            "approved": True,
            "intent": {
                "strategy_id": "quote_v1",
                "symbol": "BTCUSDT",
                "side": "long",
            },
            "quote_event_ts": "2026-01-01T00:45:00+00:00",
        },
    }
    live_record = {
        "kind": "shadow_intent",
        "payload": {
            **replay_record["payload"],
            "intent_key": "quote_v1|BTC/USDT:USDT|long|1000",
            "intent": {
                "strategy_id": "quote_v1",
                "symbol": "BTC/USDT:USDT",
                "side": "long",
            },
        },
    }
    replay = {
        "strategy_id": "quote_v1",
        "symbol": "BTCUSDT",
        "source_window": {
            "start": "2026-01-01T00:00:00+00:00",
            "end_exclusive": "2026-01-01T01:00:00+00:00",
        },
        "records": [replay_record],
        "approval_parity_eligible": True,
    }

    result = scanner_evidence.compare_quote_replay_to_live(replay, [live_record])

    assert result["exact_parity"] is True
    assert result["matched_intents"] == 1


def test_live_parity_rejects_external_book_capture_even_when_intents_match():
    replay = {
        "strategy_id": "quote_v1",
        "symbol": "BTCUSDT",
        "source_window": {
            "start": "2026-01-01T00:00:00+00:00",
            "end_exclusive": "2026-01-01T01:00:00+00:00",
        },
        "capture_quality": {
            "mode": "external_book",
            "queue_overflow_drops": 0,
            "complete": True,
            "parity_eligible": False,
        },
        "records": [],
    }

    result = scanner_evidence.compare_quote_replay_to_live(replay, [])

    assert result["exact_parity"] is False
    assert result["input_eligible"] is False
    assert result["input_ineligible_reasons"] == [
        "quote_capture_not_parity_eligible:external_book",
        "approval_fire_guard_not_replayed",
    ]


def test_load_evidence_frame_concatenates_shard_directories(tmp_path: Path):
    day = tmp_path / "stream=book" / "20260820"
    day.mkdir(parents=True)
    pd.DataFrame({"ts_ms": [2], "bid": [1.0], "ask": [1.1]}).to_parquet(day / "b.parquet")
    pd.DataFrame({"ts_ms": [1], "bid": [0.9], "ask": [1.0]}).to_parquet(day / "a.parquet")

    frame = scanner_evidence.load_evidence_frame(tmp_path)

    assert len(frame) == 2
    assert set(frame.columns) == {"ts_ms", "bid", "ask"}


def test_quote_replay_uses_shared_approval_guard(monkeypatch):
    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _quote_strategy_class())
    monkeypatch.setattr(scanner_evidence, "scanner_runtime_contract", lambda _: _quote_contract())
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 101.1], "high": [100.5, 101.3],
            "low": [99.5, 100.8], "close": [100.0, 101.15],
            "volume": [10.0, 12.0],
        }
    )
    boundary = pd.Timestamp("2026-01-01T00:05:00Z")
    quotes = pd.DataFrame(
        {
            "ts_ms": [int(boundary.timestamp() * 1000), int(boundary.timestamp() * 1000) + 600],
            "bid": [101.09, 101.10], "ask": [101.10, 101.11], "sequence": [1, 2],
        }
    )

    result = scanner_evidence.replay_quote_scanner(
        "test_quote_scanner",
        candles,
        quotes,
        approve_fire=lambda fire, index, ts: ScannerApproval(
            approved=False,
            intent={},
            failed_checks=("cost_gate:test",),
            explanation="test reject",
            intent_key=f"test_quote_scanner|BTCUSDT|{fire.side}|{int(ts.timestamp() * 1000)}",
        ),
    )

    intent = next(row for row in result["records"] if row["kind"] == "shadow_intent")
    assert intent["payload"]["approved"] is False
    assert intent["payload"]["failed_checks"] == ["cost_gate:test"]
    assert result["approval_parity_eligible"] is True
    assert "risk_gateway_not_replayed" not in result["performance_blockers"]


def test_lane_consumed_quote_replay_preserves_invalid_rows(monkeypatch):
    monkeypatch.setattr(scanner_evidence, "get_strategy_class", lambda _: _quote_strategy_class())
    monkeypatch.setattr(scanner_evidence, "scanner_runtime_contract", lambda _: _quote_contract())
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC"),
            "open": [100.0, 100.0], "high": [101.0, 101.0],
            "low": [99.0, 99.0], "close": [100.0, 100.0], "volume": [1.0, 1.0],
        }
    )
    ts_ms = int(pd.Timestamp("2026-01-01T00:05:00Z").timestamp() * 1000)
    quotes = pd.DataFrame(
        {
            "ts_ms": [ts_ms], "received_ts_ms": [ts_ms],
            "bid": [0.0], "ask": [0.0], "sequence": [1],
            "lane_id": ["lane"], "captured_at_ms": [1], "capture_overflow_drops": [0],
        }
    )

    result = scanner_evidence.replay_quote_scanner("test_quote_scanner", candles, quotes)

    assert result["quotes_clean"] == 1
    assert result["quotes_dropped"] == 0
    assert any(
        row["kind"] == "scanner_transition"
        and row["payload"].get("state") == "invalid_quote"
        for row in result["records"]
    )


def test_journal_report_counts_transitions_and_unmatched_outcome(tmp_path: Path):
    journal = tmp_path / "lifecycle.journal.jsonl"
    records = [
        {"kind": "scanner_transition", "payload": {
            "strategy_id": "quote_v1", "symbol": "BTCUSDT",
            "state": "armed_long", "episode_id": 7,
        }},
        {"kind": "shadow_outcome", "payload": {
            "intent_key": "quote_v1|BTCUSDT|long|1",
            "strategy_id": "quote_v1", "symbol": "BTCUSDT",
            "captured_bps": 10.0, "gross_pnl_usd": 3.0,
            "fees_usd": 1.0, "virtual_net_usd": 2.0,
        }},
    ]
    journal.write_text("\n".join(json.dumps(row) for row in records) + "\n")

    report = scanner_evidence.build_journal_report([journal])
    row = report["strategies"][0]

    assert row["armed_entries"] == 1
    assert row["quote_lifecycle"] == {"armed_long": 1}
    assert row["virtual_resolved"] == 1
    assert row["unmatched_outcomes"] == 1
    assert row["observed_shadow_net_usd"] == 2.0
    assert row["net_execution_usd"] == 2.0
    assert report["scanner_transitions"] == 1
    assert report["quote_lifecycle"] == {"armed_long": 1}
    assert report["virtual_approved"] == 0
    assert report["virtual_rejected"] == 0
    assert report["virtual_pending"] == 0
    assert report["gross_usd"] == 3.0
    assert report["fees_usd"] == 1.0
    assert report["observed_shadow_net_usd"] == 2.0
    assert report["net_execution_usd"] == 2.0
    assert "unmatched_outcome_lifecycle" in report["performance_blockers"]
