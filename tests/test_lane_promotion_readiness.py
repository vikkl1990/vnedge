"""Lane promotion readiness: firing vs paper/live eligibility."""

import json

from vnedge.research.lane_promotion_readiness import (
    STATUS_PAPER_REVIEW_READY,
    STATUS_REPLAY_NEEDS_ADAPTER,
    STATUS_SHADOW_NOT_FIRING,
    ReadinessConfig,
    build_lane_promotion_readiness,
    publish_readiness,
)
from vnedge.research.shadow_manifest import generate_shadow_manifest, write_shadow_manifest


STRATEGY = "funding_mean_reversion_v1"
SYMBOL = "BTC/USDT:USDT"


def _pair(exchange="binanceusdm", symbol=SYMBOL, strategy=STRATEGY):
    return {
        "exchange": exchange,
        "symbol": symbol,
        "best_strategy": strategy,
        "timeframe": "1h",
        "verdict": "PASS",
        "oos_net_usd": 20.0,
    }


def _intent(key):
    return {
        "ts": "2026-07-01T00:00:00+00:00",
        "kind": "shadow_intent",
        "payload": {
            "intent_key": key,
            "approved": True,
            "intent": {
                "symbol": SYMBOL,
                "side": "long",
                "quantity": 0.01,
                "notional_usd": 500.0,
                "leverage": 1.0,
                "reduce_only": False,
                "strategy_id": STRATEGY,
            },
        },
    }


def _outcome(key, net, ts):
    return {
        "ts": ts,
        "kind": "shadow_outcome",
        "payload": {
            "intent_key": key,
            "resolution": "target" if net > 0 else "stop",
            "virtual_net_usd": net,
            "bar_ts": ts,
        },
    }


def _write_journal(path, records):
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")


def test_manifest_lane_without_shadow_outcomes_is_not_firing(tmp_path):
    research = tmp_path / "research"
    journals = tmp_path / "logs"
    write_shadow_manifest(generate_shadow_manifest([_pair()]), research)

    payload = build_lane_promotion_readiness(research_dir=research, journal_dir=journals)

    assert payload["summary"]["shadow_not_firing"] == 1
    assert payload["summary"]["paper_review_ready"] == 0
    row = payload["rows"][0]
    assert row["status"] == STATUS_SHADOW_NOT_FIRING
    assert row["paper_review_ready"] is False
    assert "no resolved shadow_outcome" in row["blockers"][0]
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_positive_mature_shadow_lane_is_paper_review_ready_not_live_ready(tmp_path):
    research = tmp_path / "research"
    journals = tmp_path / "logs"
    research.mkdir()
    journals.mkdir()
    write_shadow_manifest(generate_shadow_manifest([_pair()]), research)
    _write_journal(
        journals / "funding_mr_binanceusdm_btc_usdt_usdt_shadow.journal.jsonl",
        [
            _intent("k1"),
            _intent("k2"),
            _outcome("k1", 10.0, "2026-07-01T00:00:00+00:00"),
            _outcome("k2", -2.0, "2026-07-03T00:00:00+00:00"),
        ],
    )

    payload = build_lane_promotion_readiness(
        research_dir=research,
        journal_dir=journals,
        config=ReadinessConfig(
            min_shadow_trades=2,
            min_shadow_span_days=2.0,
            min_shadow_profit_factor=1.25,
        ),
    )

    row = payload["rows"][0]
    assert row["status"] == STATUS_PAPER_REVIEW_READY
    assert row["paper_review_ready"] is True
    assert row["live_ready"] is False
    assert row["can_promote"] is False
    assert payload["summary"]["paper_review_ready"] == 1
    assert payload["summary"]["live_ready"] == 0
    assert "paper trial not completed" in row["live_blockers"]


def test_filtered_replay_trial_is_adapter_blocked(tmp_path):
    research = tmp_path / "research"
    filtered = {
        "rows": [
            {
                "candidate_id": "event_leadlag|bybit|SOL/USDT:USDT|20260710|buy",
                "source": "event_leadlag",
                "family": "delta_follower_v1",
                "exchange": "bybit",
                "symbol": "SOL/USDT:USDT",
                "day": "20260710",
                "side": "buy",
                "verdict": "REPLAY_CANDIDATE",
                "quotes": 2,
                "fills": 2,
                "net_usd": 3.5,
                "avg_net_bps": 12.0,
                "profit_factor": 3.0,
            }
        ]
    }
    write_shadow_manifest(generate_shadow_manifest([], filtered_replay_payload=filtered), research)

    payload = build_lane_promotion_readiness(research_dir=research, journal_dir=tmp_path / "none")

    row = payload["rows"][0]
    assert row["status"] == STATUS_REPLAY_NEEDS_ADAPTER
    assert row["paper_review_ready"] is False
    assert row["evidence"]["filtered_replay"]["verdict"] == "REPLAY_CANDIDATE"
    assert "no runtime shadow adapter" in row["blockers"][0]
    assert payload["summary"]["filtered_replay_shadow_trials"] == 1


def test_publish_readiness_is_atomic_and_appends_feed(tmp_path):
    payload = {"summary": {"total_rows": 0}, "can_trade": False, "can_promote": False}
    out = tmp_path / "readiness.json"
    feed = tmp_path / "feed.jsonl"

    publish_readiness(payload, out, feed)
    publish_readiness(payload, out, feed)

    assert json.loads(out.read_text())["summary"]["total_rows"] == 0
    assert not list(tmp_path.glob("*.tmp"))
    assert len(feed.read_text().strip().splitlines()) == 2
