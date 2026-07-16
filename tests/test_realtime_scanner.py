import json
from datetime import UTC, datetime, timedelta

from vnedge.research.realtime_scanner import (
    STATE_FIRING,
    STATE_NEAR_TRIGGER,
    STATE_WAITING,
    RealtimeScannerConfig,
    build_realtime_scanner,
    publish_realtime_scanner,
)


NOW = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, default=str) + "\n")


def record(kind, payload, minutes_ago=1):
    return {
        "ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "kind": kind,
        "payload": payload,
    }


def lane_eval(*, fired=False, funding=0.62, z=0.4, mode="shadow"):
    return {
        "bar_ts": (NOW - timedelta(minutes=1)).isoformat(),
        "strategy_id": "funding_mean_reversion",
        "symbol": "BTC/USDT:USDT",
        "mode": mode,
        "fired": fired,
        "signal_reason": "funding stretched" if fired else None,
        "skip_reason": None,
        "features": {"funding_pct": funding, "close_z": z},
        "thresholds": {"extreme_pct": 0.85, "z_entry": 1.5},
        "backfill": False,
    }


def test_runtime_lane_waiting_with_threshold_gap(tmp_path):
    journal = tmp_path / "logs" / "funding_mr_binanceusdm_btc_shadow.journal.jsonl"
    write_jsonl(journal, [record("lane_eval", lane_eval(funding=0.62, z=-0.4))])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    assert payload["mode"] == "live_observation_not_replay"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["waiting"] == 1
    row = payload["rows"][0]
    assert row["state"] == STATE_WAITING
    assert row["exchange"] == "binanceusdm"
    assert "funding" in row["why"]
    assert row["proximity"][0]["name"] == "funding"
    assert row["requires_replay_for_promotion"] is True


def test_runtime_lane_near_trigger(tmp_path):
    journal = tmp_path / "logs" / "funding_mr_bybit_btc_shadow.journal.jsonl"
    write_jsonl(journal, [record("lane_eval", lane_eval(funding=0.82, z=0.2))])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        config=RealtimeScannerConfig(near_trigger_ratio=0.90),
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["state"] == STATE_NEAR_TRIGGER
    assert payload["summary"]["near_trigger"] == 1
    assert "96%" in row["why"]


def test_runtime_lane_reports_quant_score_proximity(tmp_path):
    journal = tmp_path / "logs" / "quant_signal_pack_v1_bybit_eth_shadow.journal.jsonl"
    write_jsonl(journal, [
        record(
            "lane_eval",
            {
                "bar_ts": (NOW - timedelta(minutes=1)).isoformat(),
                "strategy_id": "quant_signal_pack_v1",
                "symbol": "ETH/USDT:USDT",
                "mode": "shadow",
                "fired": False,
                "signal_reason": None,
                "skip_reason": None,
                "features": {
                    "long_score": 4.5,
                    "short_score": 3.9,
                    "volume_z": 0.2,
                },
                "thresholds": {
                    "min_score": 5.0,
                    "min_score_delta": 1.0,
                    "min_volume_z": 0.35,
                },
                "backfill": False,
            },
        )
    ])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        config=RealtimeScannerConfig(near_trigger_ratio=0.95),
        now=NOW,
    )

    row = payload["rows"][0]
    names = {item["name"] for item in row["proximity"]}
    assert row["state"] == STATE_WAITING
    assert row["why"].startswith("score 4.5/5")
    assert {"score", "score_delta", "volume_z"} <= names
    assert row["gate_diagnostics"]["primary_blocker"]["name"] == "score"
    assert row["gate_diagnostics"]["primary_blocker"]["category"] == "confluence_quality"
    assert row["uplift"]["action"] == "ISOLATE_STRONGER_SIGNAL_FAMILY"


def test_runtime_lane_with_passed_gates_and_cooldown_reports_runtime_blocker(tmp_path):
    journal = tmp_path / "logs" / "quant_signal_pack_v1_bybit_eth_shadow.journal.jsonl"
    write_jsonl(journal, [
        record(
            "lane_eval",
            {
                "bar_ts": (NOW - timedelta(minutes=1)).isoformat(),
                "strategy_id": "quant_signal_pack_v1",
                "symbol": "ETH/USDT:USDT",
                "mode": "shadow",
                "fired": False,
                "signal_reason": None,
                "skip_reason": "post_exit_cooldown: 1 bar(s) remaining",
                "features": {
                    "long_score": 7.0,
                    "short_score": 1.0,
                    "volume_z": 1.2,
                },
                "thresholds": {
                    "min_score": 5.0,
                    "min_score_delta": 1.0,
                    "min_volume_z": 0.35,
                },
                "backfill": False,
            },
        )
    ])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["state"] == STATE_WAITING
    assert row["gate_diagnostics"]["all_gates_passed"] is True
    assert row["gate_diagnostics"]["primary_blocker"] is None
    assert row["uplift"]["action"] == "WAIT_FOR_COOLDOWN_CLEAR"
    assert row["uplift"]["priority"] == "observe"


def test_runtime_lane_reports_sats_and_stealth_proximity(tmp_path):
    journal = tmp_path / "logs" / "sats_5m_scalper_delta_sol_shadow.journal.jsonl"
    write_jsonl(journal, [
        record(
            "lane_eval",
            {
                "bar_ts": (NOW - timedelta(minutes=1)).isoformat(),
                "strategy_id": "sats_5m_scalper_v1",
                "symbol": "SOL/USD:USD",
                "mode": "shadow",
                "fired": False,
                "signal_reason": None,
                "skip_reason": None,
                "features": {
                    "tqi_long": 0.51,
                    "tqi_short": 0.30,
                    "quality_strength": 0.21,
                    "mom_persist_long": 0.44,
                    "bbp": 0.08,
                    "volume_z": -0.90,
                    "expected_net_edge_bps_long": 22.0,
                },
                "thresholds": {
                    "min_tqi": 0.58,
                    "min_quality_strength": 0.08,
                    "min_momentum_persistence": 0.55,
                    "min_bbp_atr": 0.10,
                    "min_volume_z": -0.75,
                    "min_expected_net_edge_bps": 25.0,
                },
                "backfill": False,
            },
        )
    ])

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        config=RealtimeScannerConfig(near_trigger_ratio=0.95),
        now=NOW,
    )

    row = payload["rows"][0]
    names = {item["name"] for item in row["proximity"]}
    assert row["state"] == STATE_WAITING
    assert "expected_net_edge_bps" in names
    assert {"tqi", "quality_strength", "momentum_persistence", "bbp_atr", "volume_z"} <= names
    assert row["gate_diagnostics"]["primary_blocker"]["name"] == "expected_net_edge_bps"
    assert row["gate_diagnostics"]["primary_blocker"]["category"] == "cost_edge"
    assert row["uplift"]["action"] == "REPAIR_EXECUTION_ROUTE_OR_SKIP"


def test_runtime_lane_firing_counts_shadow_intent(tmp_path):
    journal = tmp_path / "logs" / "funding_mr_delta_india_btc_shadow.journal.jsonl"
    write_jsonl(
        journal,
        [
            record("lane_eval", lane_eval(fired=True, funding=0.91, z=0.1)),
            record(
                "shadow_intent",
                {
                    "intent_key": "k1",
                    "approved": True,
                    "intent": {
                        "exchange": "delta_india",
                        "symbol": "BTC/USD:USD",
                        "side": "long",
                        "notional_usd": 500.0,
                        "strategy_id": "funding_mean_reversion",
                    },
                    "signal_reason": "funding stretched",
                },
            ),
        ],
    )

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["state"] == STATE_FIRING
    assert row["funnel"]["live_signals"] == 1
    assert row["funnel"]["shadow_intents"] == 1
    assert row["latest_shadow_intent"]["approved"] is True
    assert payload["summary"]["firing"] == 1


def test_runtime_paper_lane_reports_order_and_exit_activity(tmp_path):
    journal = tmp_path / "logs" / "funding_mr_btc_v1_20260703.journal.jsonl"
    write_jsonl(
        journal,
        [
            record("lane_eval", lane_eval(fired=True, funding=0.91, z=-1.8, mode="paper")),
            record("risk_decision", {"approved": True}),
            record(
                "order_intent",
                {
                    "intent_key": "k-paper",
                    "client_order_id": "vne_123",
                    "intent": {
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                        "quantity": 0.01,
                        "strategy_id": "funding_mean_reversion",
                        "reduce_only": False,
                    },
                },
            ),
            record("order_acknowledged", {"intent_key": "k-paper"}),
            record("live_paper_exit", {"reason": "take_profit", "state": "filled"}),
        ],
    )

    payload = build_realtime_scanner(
        research_dir=tmp_path / "research",
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["mode"] == "paper"
    assert row["state"] == STATE_FIRING
    assert row["funnel"]["paper_order_intents"] == 1
    assert row["funnel"]["paper_exits"] == 1
    assert row["latest_paper_order"]["client_order_id"] == "vne_123"
    assert payload["summary"]["paper_lanes"] == 1
    assert payload["summary"]["paper_firing"] == 1
    assert payload["summary"]["paper_order_intents"] == 1
    assert "Paper activity: 1 lane(s), 1 order intents, 1 exits" in payload["operator_answer"]


def test_event_leadlag_shadow_artifact_is_live_scanner_row(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    (research / "event_leadlag_shadow_latest.json").write_text(json.dumps({
        "generated_at": NOW.isoformat(),
        "evaluations": [{
            "runner_id": "event_leadlag_shadow_runner",
            "spec_id": "sol_binance_to_delta_long",
            "leader_exchange": "binanceusdm",
            "leader_symbol": "SOL/USDT:USDT",
            "follower_exchange": "delta_india",
            "follower_symbol": "SOL/USD:USD",
            "fired": False,
            "state": "NO_TRADE",
            "why_no_trade": ["leader_move_below: 3bps < 20bps"],
            "metrics": {
                "signed_leader_bps": 3.0,
                "signed_leader_z": 0.5,
                "leader_volume_z": 0.7,
                "filter": {
                    "min_abs_leader_bps": 20.0,
                    "min_abs_leader_z": 1.0,
                    "min_volume_z": 1.0,
                },
            },
        }],
    }))

    payload = build_realtime_scanner(
        research_dir=research,
        journal_dir=tmp_path / "logs",
        now=NOW,
    )

    row = payload["rows"][0]
    assert row["row_type"] == "event_leadlag_shadow"
    assert row["state"] == STATE_WAITING
    assert row["exchange"] == "delta_india"
    assert "leader_move_below" in row["why"]
    assert row["gate_diagnostics"]["primary_blocker"]["category"] == "participation"
    assert row["uplift"]["action"] == "WAIT_FOR_REAL_PARTICIPATION"
    assert payload["summary"]["event_lanes"] == 1
    assert payload["summary"]["top_blocker_categories"]["participation"] == 1


def test_publish_realtime_scanner_atomic(tmp_path):
    payload = {"summary": {"total_rows": 0}, "can_trade": False, "can_promote": False}
    out = tmp_path / "research" / "realtime_scanner_latest.json"
    feed = tmp_path / "research" / "realtime_scanner_feed.jsonl"

    publish_realtime_scanner(payload, out, feed)

    assert json.loads(out.read_text())["can_trade"] is False
    assert json.loads(feed.read_text().splitlines()[0])["can_promote"] is False
