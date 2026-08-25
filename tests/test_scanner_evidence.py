import json
from pathlib import Path

import pandas as pd

from vnedge.research import scanner_evidence
from vnedge.research.scanner_evidence import build_daily_report, read_lane_evals
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.realtime_entry import RealtimeEntryArm
from vnedge.strategy.scanner_contracts import ScannerRuntimeContract
from vnedge.strategy.squeeze_expansion_breakout_v3 import SqueezeExpansionV3Params


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
    assert outcome["net_execution_bps"] > outcome["net_gate_bps"]


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
    assert result["intents"] == 1
    assert result["intent_keys"] == [
        "test_quote_scanner|BTC/USDT:USDT|long|1767225900600"
    ]
    intent = next(
        row["payload"] for row in result["records"] if row["kind"] == "shadow_intent"
    )
    assert intent["entry_price"] == 101.11
    assert intent["quote_sequence"] == 2


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
    assert row["virtual_resolved"] == 1
    assert row["virtual_pending"] == 0
    assert row["gross_usd"] == 10.0
    assert row["net_execution_usd"] == 9.7


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
    assert report["fires"] == 1
