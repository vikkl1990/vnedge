"""Persistent alpha workbench."""

import json

from vnedge.research.alpha_workbench import (
    build_proof_tasks,
    publish_alpha_workbench,
    run_alpha_workbench,
)


def write_json(path, payload):
    path.write_text(json.dumps(payload))


def debate(action, candidate_id, *, source="event_leadlag_alpha", priority=88.0, vetoes=None):
    return {
        "candidate": {
            "candidate_id": candidate_id,
            "source": source,
            "family": "cross_venue_event_leadlag_v1",
            "exchange": "delta_india",
            "symbol": "SOL/USD:USD",
            "timeframe": "15m",
            "state": "EDGE_CANDIDATE_MAKER",
            "route_decision": "MAKER_ONLY",
            "metrics": {"samples": 31, "maker_avg_net_bps": 9.4},
            "evidence": {"hypothesis_id": candidate_id, "samples": 31},
        },
        "priority_score": priority,
        "council_verdict": "HIGH_PRIORITY_RESEARCH",
        "next_action": action,
        "can_trade": False,
        "can_promote": False,
        "vetoes": vetoes or ["requires_conservative_l2_replay", "maker_fill_unproven"],
        "debate": [
            {
                "agent_id": "research_director",
                "argument": "run conservative replay before any shadow discussion",
            }
        ],
    }


def council_payload(*rows):
    return {
        "generated_at": "2026-07-08T00:00:00+00:00",
        "council_id": "alpha_agent_council_v1",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "debates": list(rows),
    }


def test_alpha_workbench_turns_council_debate_into_research_task(tmp_path):
    payload = council_payload(
        debate(
            "RUN_CONSERVATIVE_L2_REPLAY",
            "event_leadlag|SOL|binanceusdm->delta_india",
        )
    )

    tasks = build_proof_tasks(payload)

    assert len(tasks) == 1
    task = tasks[0]
    assert task.task_type == "conservative_replay"
    assert task.next_action == "RUN_CONSERVATIVE_L2_REPLAY"
    assert task.source == "event_leadlag_alpha"
    assert task.can_trade is False
    assert task.can_promote is False
    assert task.live_orders_enabled is False
    assert "conservative_replay_result" in task.blocked_by
    assert "maker_fill_unproven" in task.blocked_by
    assert task.evidence_digest


def test_alpha_workbench_persists_chunks_idempotently(tmp_path):
    write_json(
        tmp_path / "alpha_council_latest.json",
        council_payload(
            debate(
                "RUN_CONSERVATIVE_L2_REPLAY",
                "event_leadlag|SOL|binanceusdm->delta_india",
            )
        ),
    )
    store = tmp_path / "alpha_workbench"

    first = run_alpha_workbench(tmp_path, store_dir=store)
    second = run_alpha_workbench(tmp_path, store_dir=store)

    assert first["summary"]["open_tasks"] == 1
    assert first["persistence"]["new_tasks"] == 1
    assert second["persistence"]["new_tasks"] == 0
    assert second["persistence"]["unchanged_tasks"] == 1
    chunks = list((store / "chunks").glob("*.json"))
    assert len(chunks) == 1
    chunk = json.loads(chunks[0].read_text())
    assert chunk["workbench_id"] == "alpha_workbench_v1"
    assert chunk["task"]["can_trade"] is False
    manifest = json.loads((store / "manifest.json").read_text())
    record = next(iter(manifest["tasks"].values()))
    assert record["times_seen"] == 2
    assert record["status"] == "OPEN"
    assert record["can_promote"] is False


def test_alpha_workbench_routes_judgment_and_recording_tasks(tmp_path):
    payload = council_payload(
        debate(
            "PRE_REGISTER_UNTOUCHED_JUDGMENT",
            "candle|delta|XRP|1h|vol_expansion",
            source="rolling_walk_forward",
            priority=82.0,
            vetoes=["requires_untouched_judgment"],
        ),
        debate(
            "RECORD_MORE_TICKS",
            "l2_scout|delta|SOL|absorption|UNDER_SAMPLED",
            source="fast_l2_scout",
            priority=48.0,
            vetoes=["needs_more_samples"],
        ),
        debate("HOLD_RESEARCH_ONLY", "ignored|dead"),
    )

    report = run_alpha_workbench(tmp_path, store_dir=None, council_payload=payload)

    assert report["policy"]["can_trade"] is False
    assert report["policy"]["auto_promotion_allowed"] is False
    assert report["summary"]["open_tasks"] == 2
    assert report["summary"]["by_type"] == {
        "untouched_judgment": 1,
        "data_collection": 1,
    }
    assert report["tasks"][0]["task_type"] == "untouched_judgment"
    assert "human_approved_manifest" in report["tasks"][0]["blocked_by"]
    assert report["tasks"][1]["task_type"] == "data_collection"
    assert "sample_size_and_coverage" in report["tasks"][1]["blocked_by"]


def test_alpha_workbench_publish_is_atomic_and_appends_feed(tmp_path):
    payload = run_alpha_workbench(
        tmp_path,
        store_dir=None,
        council_payload=council_payload(
            debate("RECORD_MORE_TICKS", "l2_scout|delta|SOL|under")
        ),
    )
    out = tmp_path / "alpha_workbench_latest.json"
    feed = tmp_path / "alpha_workbench_feed.jsonl"

    publish_alpha_workbench(payload, out, feed)
    publish_alpha_workbench(payload, out, feed)

    assert json.loads(out.read_text())["workbench_id"] == "alpha_workbench_v1"
    assert not list(tmp_path.glob("*.tmp"))
    assert len(feed.read_text().strip().splitlines()) == 2
