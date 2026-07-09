"""Alpha Council deterministic agent debate."""

import json

from vnedge.research.alpha_council import (
    collect_candidates,
    publish_alpha_council,
    run_alpha_council,
)


def write_json(path, payload):
    path.write_text(json.dumps(payload))


def test_alpha_council_ranks_event_leadlag_but_keeps_replay_gate(tmp_path):
    write_json(
        tmp_path / "event_leadlag_latest.json",
        {
            "generated_at": "2026-07-08T00:00:00Z",
            "hypotheses": [
                {
                    "hypothesis_id": "leadlag|SOL|binance->delta",
                    "state": "EDGE_CANDIDATE_MAKER",
                    "follower_exchange": "delta_india",
                    "follower_symbol": "SOL/USD:USD",
                    "horizon_min": 15,
                    "route_decision": "MAKER_ONLY",
                    "samples": 33,
                    "maker_avg_net_bps": 11.8,
                    "maker_profit_factor": 1.98,
                    "taker_profit_factor": 1.62,
                    "win_rate_pct": 27.3,
                }
            ],
        },
    )

    payload = run_alpha_council(tmp_path)

    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["high_priority"] == 1
    debate = payload["debates"][0]
    assert debate["candidate"]["family"] == "cross_venue_event_leadlag_v1"
    assert debate["next_action"] == "RUN_CONSERVATIVE_L2_REPLAY"
    assert "requires_conservative_l2_replay" in debate["vetoes"]
    assert "maker_fill_unproven" in debate["vetoes"]
    assert {row["agent_id"] for row in debate["debate"]} == {
        "edge_advocate",
        "skeptic",
        "execution_specialist",
        "risk_governor",
        "research_director",
    }


def test_alpha_council_sends_candle_pass_to_untouched_judgment(tmp_path):
    write_json(
        tmp_path / "latest.json",
        {
            "generated_at": "2026-07-08T00:00:00Z",
            "results": [
                {
                    "exchange": "delta_india",
                    "symbol": "XRP/USD:USD",
                    "timeframe": "1h",
                    "strategy": "volatility_expansion_breakout_v1",
                    "verdict": "PASS",
                    "oos_net_usd": 23.47,
                    "oos_trades": 24,
                    "profit_factor": 1.45,
                    "payoff_ratio": 1.9,
                    "profitable_windows_pct": 71.4,
                }
            ],
        },
    )

    payload = run_alpha_council(tmp_path)

    debate = payload["debates"][0]
    assert debate["candidate"]["source"] == "rolling_walk_forward"
    assert debate["next_action"] == "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    assert "requires_untouched_judgment" in debate["vetoes"]
    assert debate["can_trade"] is False


def test_alpha_council_under_sampled_l2_candidate_records_more_ticks(tmp_path):
    write_json(
        tmp_path / "l2_scout_latest.json",
        {
            "top_results": [
                {
                    "exchange": "delta_india",
                    "symbol": "SOL/USD:USD",
                    "family": "absorption_reversal",
                    "state": "UNDER_SAMPLED",
                    "route_decision": {"route": "BLOCKED"},
                    "samples": 4,
                    "avg_net_bps": 3.2,
                    "profit_factor": 1.4,
                    "horizon_ms": 5000,
                }
            ]
        },
    )

    candidates = collect_candidates(tmp_path)
    payload = run_alpha_council(tmp_path)

    assert any(candidate.source == "fast_l2_scout" for candidate in candidates)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["source"] == "fast_l2_scout"
    )
    assert debate["candidate"]["source"] == "fast_l2_scout"
    assert debate["next_action"] == "RECORD_MORE_TICKS"
    assert "needs_more_samples" in debate["vetoes"]
    assert debate["can_trade"] is False


def test_alpha_council_routes_positive_rejects_to_specific_repair(tmp_path):
    write_json(
        tmp_path / "latest.json",
        {
            "generated_at": "2026-07-08T00:00:00Z",
            "results": [
                {
                    "exchange": "bybit",
                    "symbol": "XRP/USDT:USDT",
                    "timeframe": "1h",
                    "strategy": "trend_continuation_v1",
                    "verdict": "REJECT",
                    "oos_net_usd": 104.42,
                    "oos_trades": 48,
                    "profit_factor": 1.75,
                    "payoff_ratio": 2.07,
                    "profitable_windows_pct": 57.1,
                    "reasons": [
                        "one zero-trade test window",
                        "IS/OOS collapse retention below gate",
                    ],
                },
                {
                    "exchange": "binanceusdm",
                    "symbol": "ETH/USDT:USDT",
                    "timeframe": "1h",
                    "strategy": "quant_signal_pack_v1",
                    "verdict": "REJECT",
                    "oos_net_usd": 82.24,
                    "oos_trades": 61,
                    "profit_factor": 1.23,
                    "payoff_ratio": 1.08,
                    "reasons": ["aggregate OOS PF below gate", "payoff ratio below gate"],
                },
            ],
        },
    )

    payload = run_alpha_council(tmp_path)
    actions = {
        row["candidate"]["symbol"]: row["next_action"]
        for row in payload["debates"]
        if row["candidate"]["source"] == "rolling_walk_forward"
    }

    assert actions["XRP/USDT:USDT"] == "CHECK_ZERO_WINDOW_STABILITY"
    assert actions["ETH/USDT:USDT"] == "REPAIR_EXIT_PAYOFF"
    assert payload["summary"]["sources"]["rolling_walk_forward"] == 2


def test_alpha_council_flags_missing_research_artifacts(tmp_path):
    write_json(tmp_path / "latest.json", {"generated_at": "now", "results": []})

    payload = run_alpha_council(tmp_path)

    refreshes = [
        row for row in payload["debates"]
        if row["candidate"]["source"] == "artifact_health"
    ]
    assert refreshes
    assert all(row["next_action"] == "REFRESH_STALE_ARTIFACT" for row in refreshes)
    assert any(
        row["candidate"]["symbol"] == "daily_scalper_latest.json"
        for row in refreshes
    )


def test_alpha_council_routes_stressed_bitcoin_regime_to_replay_split(tmp_path):
    write_json(
        tmp_path / "bitcoin_regime_latest.json",
        {
            "schema_version": "bitcoin_regime_v1",
            "source": {"status": "ok"},
            "policy": {"can_trade": False, "can_promote": False},
            "summary": {
                "stress_state": "stressed",
                "stress_score": 6.75,
                "source_status": "ok",
            },
            "mempool": {"stress_state": "stressed"},
            "features": {
                "fee_pressure_score": 3.0,
                "mempool_pressure_score": 3.0,
                "fastest_fee_sat_vb": 85.0,
                "mempool_vsize_vb": 420_000_000,
                "mempool_tx_count": 240_000,
                "fee_spike_z": 2.4,
                "mempool_pressure_z": 1.2,
            },
        },
    )

    payload = run_alpha_council(tmp_path)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["source"] == "bitcoin_regime"
    )

    assert debate["candidate"]["route_decision"] == "CONTEXT_ONLY"
    assert debate["next_action"] == "SPLIT_REPLAY_BY_BTC_REGIME"
    assert debate["can_trade"] is False
    assert "context_only_no_trade" in debate["vetoes"]
    assert "context_only_no_execution" in debate["vetoes"]
    assert "requires_replay_context_split" in debate["vetoes"]


def test_alpha_council_routes_bad_bitcoin_source_to_health_refresh(tmp_path):
    write_json(
        tmp_path / "bitcoin_regime_latest.json",
        {
            "schema_version": "bitcoin_regime_v1",
            "source": {"status": "node_unsynced"},
            "summary": {
                "stress_state": "missing",
                "stress_score": 0.0,
                "source_status": "node_unsynced",
            },
            "mempool": {"stress_state": "missing"},
            "features": {},
        },
    )

    payload = run_alpha_council(tmp_path)
    debate = next(
        row for row in payload["debates"]
        if row["candidate"]["source"] == "bitcoin_regime"
    )

    assert debate["candidate"]["state"] == "BTC_SOURCE_NODE_UNSYNCED"
    assert debate["next_action"] == "REFRESH_BITCOIN_NODE_HEALTH"
    assert debate["can_promote"] is False


def test_alpha_council_publish_is_atomic_and_appends_feed(tmp_path):
    payload = run_alpha_council(tmp_path)
    out = tmp_path / "alpha_council_latest.json"
    feed = tmp_path / "alpha_council_feed.jsonl"

    publish_alpha_council(payload, out, feed)
    publish_alpha_council(payload, out, feed)

    assert json.loads(out.read_text())["council_id"] == "alpha_agent_council_v1"
    assert not list(tmp_path.glob("*.tmp"))
    assert len(feed.read_text().strip().splitlines()) == 2
