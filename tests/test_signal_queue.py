from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from vnedge.dashboard.signal_queue import SignalQueue, normalize, page, project
from vnedge.execution.evidence import DecisionEnvelope
from vnedge.strategy.arm_evidence import freeze_permission_from_row


def arm():
    permission = freeze_permission_from_row({
        "timestamp": datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        "open": 100., "high": 102., "low": 99., "close": 101.,
        "volume": 25., "quote_volume": 2525., "trade_count": 40,
        "is_closed": True, "data_quality": "ok", "candle_source": "canonical_tick_lake",
    }, decision_timeframe="15m", context_timeframes=(), allow_long=True,
        allow_short=False, reason="test")
    return DecisionEnvelope.create(strategy_id="range_expansion_realtime_v2", symbol="BTC/USD:USD",
        timeframe="15m", side="long", permission_snapshot=permission, entry_clock="quote_hold").as_dict()


def rec(kind="lane_eval", *, proof=False, second=0, **payload):
    if proof:
        envelope = arm()
        if kind in {"order_intent", "order_submitted"}:
            payload = {"intent": {"symbol": "BTC/USD:USD", "side": "long", "quantity": 1.,
                       "notional_usd": 101., "leverage": 5.}, **payload}
        payload = {"decision_id": envelope["decision_id"], "path_id": "kernel_v1",
                   "execution_evidence": {"arm_envelope": envelope}, **payload}
    return {"kind": kind, "ts": f"2026-09-05T12:15:{second:02}+00:00", "payload": payload}


def fold(*records):
    return project([e for i, record in enumerate(records) if (e := normalize("lane", record))])


def test_no_setup_does_not_mint_decision_or_plan():
    row, = fold(rec(skip_reason="regime_flat", decision_price=101, fired=False,
                    all_failed_gates=["regime_flat", "structure_not_ready"]))
    assert row["decision_id"] is None
    assert row["evaluation_id"].startswith("diagnostic:")
    assert row["entry"] is None
    assert row["stage"] == "evaluated"
    assert row["primary_reason"] == "regime_flat"
    assert row["failed_gates"] == ["regime_flat", "structure_not_ready"]
    assert row["ml_probability"] is None


def test_unproven_fire_is_identity_gap():
    row, = fold(rec(fired=True, decision_id="not-proof"))
    assert row["stage"] == "identity_gap"
    assert row["decision_id"] is None
    event = normalize("lane", rec(execution_evidence={"quote_sequence": "venue-seq-17"}))
    assert event["quote_sequence"] == "venue-seq-17"


def test_ack_is_not_fill_and_partial_updates_are_cumulative():
    records = [rec("order_intent", proof=True, client_order_id="minted"),
               rec("order_submitted", proof=True, client_order_id="minted", second=1),
               rec("order_acknowledged", proof=True, client_order_id="minted", second=2)]
    row, = fold(*records)
    assert row["stage"] == "acknowledged" and not row["has_fill"]
    update = rec("order_fill_sync", proof=True, client_order_id="minted", second=3,
                 filled_quantity=0.2, state="PARTIALLY_FILLED")
    row, = fold(*records, update, update)
    assert row["stage"] == "partially_filled"
    assert row["orders"][0]["filled_quantity"] == 0.2
    row, = fold(*records, update, rec("order_resolved", proof=True, client_order_id="minted",
                                    second=4, filled_quantity=1, state="FILLED"))
    assert row["stage"] == "filled"  # order reconciliation does not mean a closed trade
    assert row["has_fill"] and row["booked_net_usd"] is None
    assert row["performance_eligible"] is False


def test_multiple_orders_one_decision():
    records = []
    for coid in ("a", "b"):
        records.extend([rec("order_intent", proof=True, client_order_id=coid),
                        rec("order_submitted", proof=True, client_order_id=coid, second=1)])
    row, = fold(*records)
    assert len(row["orders"]) == 2


def test_private_updates_preserve_cumulative_fill_without_inventing_proof():
    records = [rec("order_intent", proof=True, client_order_id="a"),
               rec("order_submitted", proof=True, client_order_id="a", second=1),
               rec("private_order_update", proof=True, client_order_id="a", second=2,
                   state="partially_filled", filled_quantity=0.2),
               rec("private_order_update", proof=True, client_order_id="a", second=3,
                   state="partially_filled", no_state_change=True)]
    row, = fold(*records)
    assert row["stage"] == "partially_filled"
    assert row["orders"][0]["filled_quantity"] == 0.2
    row, = fold(rec("private_order_update", client_order_id="a", state="filled", filled_quantity=1))
    assert row["stage"] == "identity_gap" and not row["has_fill"]


def test_cancel_retains_partial_fill_but_timeout_remains_unresolved():
    records = [rec("order_intent", proof=True, client_order_id="a"),
               rec("order_submitted", proof=True, client_order_id="a", second=1),
               rec("order_cancel", proof=True, client_order_id="a", second=2,
                   filled_quantity=0.1, venue_state="cancelled")]
    row, = fold(*records)
    assert row["stage"] == "canceled_partial" and row["has_fill"]
    row, = fold(*records, rec("order_timeout_unknown", proof=True, client_order_id="a", second=3))
    assert row["stage"] == "timeout_unknown" and row["has_fill"]


def test_missing_ancestor_cannot_book_a_fill():
    row, = fold(rec("order_fill_sync", proof=True, client_order_id="orphan", state="FILLED", filled_quantity=1))
    assert not row["has_fill"]
    assert row["stage"] == "incomplete_order_chain"


def test_missing_instruction_and_changed_order_geometry_fail_closed():
    records = [rec("order_intent", proof=True, client_order_id="a", intent={}),
               rec("order_submitted", proof=True, client_order_id="a", second=1),
               rec("order_fill_sync", proof=True, client_order_id="a", second=2, state="FILLED", filled_quantity=1)]
    row, = fold(*records)
    assert not row["has_fill"]
    records[0] = rec("order_intent", proof=True, client_order_id="a", intent={
        "symbol": "BTC/USD:USD", "side": "long", "quantity": 2., "notional_usd": 202., "leverage": 5.})
    row, = fold(*records)
    assert row["stage"] == "identity_gap" and not row["has_fill"]


@pytest.mark.parametrize("field,value", [("side", "short"), ("snapshot_id", "bad"),
    ("decision_id", "bad"), ("entry_clock", "next_open"), ("path_id", "research_observe")])
def test_conflicting_identity_never_validates(field, value):
    row, = fold(rec(proof=True, **{field: value}))
    assert row["evidence_status"] == "conflict"
    assert row["decision_id"] is None


def test_conflict_quarantines_matching_valid_decision():
    records = [rec(proof=True), rec(proof=True, second=1, side="short")]
    rows = fold(*records)
    assert all(r["stage"] == "identity_gap" for r in rows)
    assert all(not r["has_fill"] for r in rows)


def test_research_results_never_become_execution_pnl():
    row, = fold(rec("shadow_outcome", proof=True, virtual_net_usd=12, performance_eligible=True))
    assert row["population"] == "research"
    assert row["stage"] == "research_resolved"
    assert row["research_net_usd"] == 12
    assert row["booked_net_usd"] is None and not row["has_fill"]


def test_all_reject_codes_preserved_and_secret_fields_not_copied():
    row, = fold(rec("risk_decision", proof=True, approved=False,
                   failed_checks=["cost", "size"], api_key="secret", password="hidden"))
    assert row["stage"] == "rejected"
    assert row["failed_gates"] == ["cost", "size"]
    assert "secret" not in json.dumps(row) and "password" not in json.dumps(row)


def append(path, *records):
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


RUNTIME = {"lanes": [{"lane_id": "lane"}]}


def test_incremental_index_restart_torn_tail_and_duplicate_records(tmp_path):
    path = tmp_path / "lane.journal.jsonl"
    first = rec(skip_reason="one")
    append(path, first, first)
    queue = SignalQueue(tmp_path)
    snap = queue.snapshot(RUNTIME)
    assert len(snap["rows"]) == 1
    second = json.dumps(rec(second=1, skip_reason="two"))
    with path.open("a") as handle:
        handle.write(second[:20])
    queue = SignalQueue(tmp_path)
    assert len(queue.snapshot(RUNTIME)["rows"]) == 1
    with path.open("a") as handle:
        handle.write(second[20:] + "\n")
    queue = SignalQueue(tmp_path)
    assert len(queue.snapshot(RUNTIME)["rows"]) == 2
    assert queue.snapshot({"lanes": []})["rows"] == []


def test_rotation_and_rewrite_invalidate_cached_ancestors(tmp_path):
    path = tmp_path / "lane.journal.jsonl"
    append(path, rec(proof=True))
    assert SignalQueue(tmp_path).snapshot(RUNTIME)["rows"][0]["decision_id"]
    path.rename(tmp_path / "rotated")
    append(path, rec(skip_reason="fresh"))
    snap = SignalQueue(tmp_path).snapshot(RUNTIME)
    assert len(snap["rows"]) == 1 and snap["rows"][0]["decision_id"] is None
    path.write_text(json.dumps(rec(skip_reason="other")) + "\n")
    assert SignalQueue(tmp_path).snapshot(RUNTIME)["rows"][0]["primary_reason"] == "other"


def test_bounded_tail_missing_sources_and_active_lane_filter(tmp_path):
    path = tmp_path / "lane.journal.jsonl"
    append(path, *(rec(second=i, skip_reason=str(i)) for i in range(50)))
    append(tmp_path / "retired.journal.jsonl", rec(fired=True))
    snap = SignalQueue(tmp_path, read_bytes=1024, max_events=3).snapshot(RUNTIME)
    assert 0 < len(snap["rows"]) <= 3
    assert snap["history_complete"] is False
    path.unlink()
    missing = SignalQueue(tmp_path).snapshot(RUNTIME)
    assert missing["sources"][0]["state"] == "source_unavailable"
    assert SignalQueue(tmp_path).snapshot({"lanes": [{"lane_id": "../retired"}]})["rows"] == []


def test_malformed_complete_lines_remain_visible_after_refresh(tmp_path):
    path = tmp_path / "lane.journal.jsonl"
    path.write_text("{broken}\n")
    append(path, rec(skip_reason="valid"))
    for _ in range(2):
        snap = SignalQueue(tmp_path).snapshot(RUNTIME)
        assert snap["sources"][0]["invalid_records"] == 1
        assert len(snap["rows"]) == 1


def test_idle_index_resyncs_latest_tail_and_persists_coverage_gap(tmp_path):
    path = tmp_path / "lane.journal.jsonl"
    append(path, rec("order_intent", proof=True, client_order_id="old"),
           rec("order_submitted", proof=True, client_order_id="old", second=1))
    SignalQueue(tmp_path).snapshot(RUNTIME)
    append(path, *(rec(second=i, skip_reason="backlog" + str(i)) for i in range(50)))
    append(path, rec("order_fill_sync", proof=True, client_order_id="old", second=59,
                     filled_quantity=1, state="FILLED"))
    for _ in range(2):
        snap = SignalQueue(tmp_path, read_bytes=4096).snapshot(RUNTIME)
        source, = snap["sources"]
        assert source["caught_up"] and source["remaining_bytes"] == 0
        assert source["skipped_bytes"] > 0
        assert source["resync_reason"] == "backlog_tail_resync"
        assert snap["last_event_at"].endswith("59+00:00")
        assert not any(row["has_fill"] for row in snap["rows"])
        assert any(row["stage"] == "incomplete_order_chain" for row in snap["rows"])
        assert not snap["history_complete"]


def test_cursor_stable_until_revision_changes_and_filters_partition(tmp_path):
    append(tmp_path / "lane.journal.jsonl", *(rec(second=i, skip_reason=str(i)) for i in range(4)))
    snap = SignalQueue(tmp_path).snapshot(RUNTIME)
    first = page(snap, filters={}, limit=2)
    second = page(snap, filters={}, limit=2, cursor=first["next_cursor"])
    assert not {r["row_key"] for r in first["rows"]} & {r["row_key"] for r in second["rows"]}
    assert first["summary"] == second["summary"]
    assert page(snap, filters={"population": "orders"})["summary"]["total"] == 0
    assert all("timeline" not in r for r in first["rows"])
    with pytest.raises(RuntimeError):
        page({**snap, "revision": "different"}, filters={}, limit=2, cursor=first["next_cursor"])
    for cursor in ("!", "e30=", "x" * 1000):
        with pytest.raises(ValueError):
            page(snap, filters={}, cursor=cursor)


def test_backfill_excluded_and_nonfinite_values_do_not_leak():
    assert normalize("lane", rec(backfill=True)) is None
    row, = fold(rec(entry_price="NaN", stop_price="Infinity"))
    assert row["entry"] is None and row["stop"] is None


def test_queue_routes_authenticated_read_only_and_detail_scoped(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from vnedge.dashboard.app import SnapshotProvider, create_app
    monkeypatch.setenv("DASHBOARD_PUBLIC_READ_ONLY", "0")
    append(tmp_path / "lane.journal.jsonl", rec(skip_reason="regime_flat"))
    provider = SnapshotProvider()
    provider.publish(RUNTIME)
    client = TestClient(create_app(provider, token="queue-test", journal_dir=tmp_path))
    headers = {"Authorization": "Bearer queue-test"}
    assert client.get("/api/signal-queue").status_code == 401
    response = client.get("/api/signal-queue", headers=headers)
    assert response.status_code == 200 and response.json()["can_trade"] is False
    row = response.json()["rows"][0]
    detail = client.get("/api/signal-queue/" + row["row_key"], headers=headers)
    assert detail.json()["row"]["timeline"][0]["reason"] == "regime_flat"
    assert client.post("/api/signal-queue", headers=headers, json={"trade": True}).status_code == 405
    assert client.get("/api/signal-queue?limit=1000", headers=headers).status_code == 400
    assert client.get("/api/signal-queue?cursor=bad", headers=headers).status_code == 400
    assert client.get("/api/signal-queue/" + "0" * 64, headers=headers).status_code == 404
    provider.publish({"lanes": []})
    assert client.get("/api/signal-queue/" + row["row_key"], headers=headers).status_code == 404
