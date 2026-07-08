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

    assert len(candidates) == 1
    debate = payload["debates"][0]
    assert debate["candidate"]["source"] == "fast_l2_scout"
    assert debate["next_action"] == "RECORD_MORE_TICKS"
    assert "needs_more_samples" in debate["vetoes"]
    assert debate["can_trade"] is False


def test_alpha_council_publish_is_atomic_and_appends_feed(tmp_path):
    payload = run_alpha_council(tmp_path)
    out = tmp_path / "alpha_council_latest.json"
    feed = tmp_path / "alpha_council_feed.jsonl"

    publish_alpha_council(payload, out, feed)
    publish_alpha_council(payload, out, feed)

    assert json.loads(out.read_text())["council_id"] == "alpha_agent_council_v1"
    assert not list(tmp_path.glob("*.tmp"))
    assert len(feed.read_text().strip().splitlines()) == 2
