"""Stage associations cannot backdate information or invent profitable trades."""
from copy import deepcopy
from datetime import timedelta

import pytest

from vnedge.dashboard.analyst_store import AnalystStore, digest
from vnedge.dashboard.official_market_stage import record_stage
from vnedge.dashboard.signal_queue import normalize, project
from vnedge.research import stage_outcomes as s
from tests.test_official_market_stage import NOW, source


@pytest.fixture
def stores(tmp_path):
    output = AnalystStore(tmp_path / "out.sqlite", writable=True, wal=False)
    official = AnalystStore(tmp_path / "official.sqlite", writable=True, wal=False)
    for tf in ("4h", "1d"):
        source(official, tf=tf)
        record_stage(official, "BTCUSD", tf, NOW)
    return output, official


def row(at=NOW + timedelta(minutes=15), **changes):
    identity = dict(strategy_id="fixture_v1", exchange="delta_india", symbol="BTC/USD:USD",
                    timeframe="15m", entry_clock="next_open", mode="paper")
    event = {**identity, "event_id": "anchor", "kind": "lane_eval", "observed_at": at.isoformat()}
    return {**identity, "row_key": digest(["decision", "lane", "decision"]), "lane": "lane",
            "decision_id": "decision", "evaluation_id": None, "envelope": {"proof": "fixture"},
            "decision_close": at.isoformat(), "observed_at": at.isoformat(),
            "evidence_status": "bound", "timeline": [event], **changes}


def label(r, **changes):
    at = r["decision_close"]
    body = {**{k: r[k] for k in s.IDENTITY}, "decision_id": r["decision_id"],
            "arm_envelope": r["envelope"], "entry_at": at, "exit_at": at, "available_at": at,
            "cost_profile_id": "cost_v1", "cost_config_sha256": "c"*64,
            "label_contract": "reconciled_paper_net_positive_v1",
            "net_usd": 1.0, "net_bps": 10.0, "gross_usd": 1.2, "fees_usd": 0.2, "funding_usd": 0.0,
            **changes}
    return {**body, "label_hash": digest(body)}


def test_asof_capture_write_once_and_no_scanner_mutation(stores):
    output, official = stores
    r = row()
    original = deepcopy(r)
    rec, state = s.bind(output, official, r, NOW, NOW+timedelta(minutes=16))
    assert state == "captured" and r == original
    assert {x["stage"] for x in rec["body"]["stages"]} == {"advancing_trend"}
    assert all(x["source"] == "official_delta_ohlc" for x in rec["body"]["stages"])
    source(official, prices=range(400,300,-1), receipt=NOW+timedelta(hours=1))
    record_stage(official, "BTCUSD", "4h", NOW+timedelta(hours=1))
    again, state = s.bind(output, official, r, NOW, NOW+timedelta(hours=2))
    assert state == "already_bound" and again == rec
    assert not rec["body"]["can_trade"]


def test_late_receipt_never_backdates_stage(stores):
    output, official = stores
    r = row(NOW-timedelta(seconds=1))
    rec, _ = s.bind(output, official, r, NOW-timedelta(hours=1), NOW)
    assert all(x["state"] == "unavailable" for x in rec["body"]["stages"])


@pytest.mark.parametrize("change,reason", [({"evidence_status":"conflict"},"identity_conflict"),
                                          ({"symbol":"BTC/USDT:USDT"},"unsupported_or_incomplete_identity"),
                                          ({"exchange":"binanceusdm"},"unsupported_or_incomplete_identity"),
                                          ({"decision_close":None},"unsupported_or_incomplete_identity"),
                                          ({"timeline":[]},"entry_anchor_missing")])
def test_bad_identity_never_captures(stores, change, reason):
    out, off = stores
    rec, why = s.bind(out, off, row(**change), NOW, NOW+timedelta(hours=1))
    assert rec is None and why == reason


def test_activation_future_and_stale_context(stores):
    out, off = stores
    assert s.bind(out, off, row(), NOW+timedelta(hours=1), NOW+timedelta(hours=2))[1] == "before_activation"
    assert s.bind(out, off, row(), NOW, NOW)[1] == "invalid_decision_time"
    rec, _ = s.bind(out, off, row(NOW+timedelta(days=2)), NOW, NOW+timedelta(days=2))
    assert all(x["state"] == "stale" for x in rec["body"]["stages"])


def test_verified_outcomes_exact_join_no_virtual_pnl_and_partition(stores):
    out, off = stores
    r = row()
    s.bind(out, off, r, NOW, NOW+timedelta(hours=1))
    audit = {"state":"VERIFIED", "labels":[label(r)], "source_hash":"chain"}
    matches, issues = s.match_outcomes(out, "lane", audit, NOW+timedelta(hours=2))
    assert len(matches) == 1 and not issues
    groups = s.aggregate(matches)
    assert len(groups) == 3 and all(g["state"] == "INSUFFICIENT" and g["profit_factor"] is None for g in groups)
    assert all(g["mean_net_bps"] == 10 for g in groups)
    second = deepcopy(matches[0]); second["label"]["cost_profile_id"] = "other_cost"
    assert len(s.aggregate([*matches, second])) == 6
    assert s.match_outcomes(out,"other_lane",audit,NOW+timedelta(hours=2))[1]["no_forward_stage_binding"] == 1
    assert s.match_outcomes(out,"lane",{**audit,"state":"BLOCKED"},NOW+timedelta(hours=2))[0] == []


@pytest.mark.parametrize("change", [{"symbol":"ETH/USD:USD"}, {"entry_clock":"quote_hold"},
                                   {"mode":"shadow"}, {"arm_envelope":{"proof":"changed"}},
                                   {"available_at":"2030-01-01T00:00:00+00:00"}, {"cost_profile_id":""}])
def test_outcome_conflicts_fail_closed(stores, change):
    out, off = stores; r = row()
    s.bind(out, off, r, NOW, NOW+timedelta(hours=1))
    matches, issues = s.match_outcomes(out,"lane",{"state":"VERIFIED","labels":[label(r,**change)]},NOW+timedelta(hours=2))
    assert not matches and issues


def test_duplicate_outcomes_invalidate_lane(stores):
    out, off = stores; r = row()
    s.bind(out, off, r, NOW, NOW+timedelta(hours=1))
    matches, issues = s.match_outcomes(out,"lane",{"state":"VERIFIED","labels":[label(r),label(r)]},NOW+timedelta(hours=2))
    assert not matches and issues["duplicate_decision_outcomes"] == 1


def test_worker_has_separate_index_persistent_activation_and_readonly_api(tmp_path):
    journals = tmp_path / "logs"; journals.mkdir()
    worker = s.StageWorker(journals,tmp_path/"absent.sqlite",tmp_path/"out"/"evidence.sqlite")
    a = worker.cycle(NOW)
    b = worker.cycle(NOW+timedelta(hours=1))
    assert a["activated_at"] == b["activated_at"]
    assert b["verified_outcomes"] == 0 and not b["groups"]
    assert not list(journals.iterdir())
    assert s.report(worker.store.path,"BTCUSD","delta_india",NOW+timedelta(hours=2))["state"] == "stale"
    assert s.report(tmp_path/"missing","BTCUSD","delta_india",NOW)["state"] == "worker_not_started"
    with pytest.raises(ValueError):
        s.report(worker.store.path,"BTCUSDT","delta_india",NOW)


def test_order_metadata_requires_exact_prior_eval():
    r = row()
    event = r["timeline"][0]
    event.update(lane="lane", decision_close=r["decision_close"])
    order = deepcopy(r); order.update(exchange=None,mode=None)
    order["timeline"][0].update(kind="order_intent",exchange=None,mode=None,event_id="order")
    enriched = s.attach_lane_metadata([r, order])[1]
    assert enriched["exchange"] == "delta_india" and enriched["timeline"][0]["metadata_event_id"] == "anchor"
    order["timeline"][0]["decision_close"] = (NOW+timedelta(minutes=30)).isoformat()
    assert s.attach_lane_metadata([r,order])[1]["exchange"] is None


def test_real_paper_ledger_end_to_end_and_corruption_withdraws_results(tmp_path, monkeypatch):
    from tests.test_ml_lab_pipeline import Clock, add_episode, envelope
    from vnedge.execution.fill_ledger import FillLedger
    from vnedge.execution.journal import DecisionJournal
    import vnedge.execution.journal as journal_module
    monkeypatch.setattr(journal_module, "datetime", Clock)
    journals = tmp_path / "logs"; journals.mkdir()
    off = AnalystStore(tmp_path/"official.sqlite", writable=True, wal=False)
    for tf in ("4h", "1d"):
        source(off, tf=tf); record_stage(off,"BTCUSD",tf,NOW)
    worker = s.StageWorker(journals, off.path, tmp_path/"out"/"evidence.sqlite")
    worker.cycle(NOW)
    j = DecisionJournal(journals/"lane.journal.jsonl")
    f = FillLedger(journals/"lane.fills.jsonl")
    env = envelope(NOW)
    t = env.permission_snapshot.decision_bar.close_time
    Clock.instant = t
    j.append("lane_eval", {"strategy_id":env.strategy_id,"exchange":"delta_india",
                          "symbol":env.symbol,"timeframe":env.timeframe,"entry_clock":env.entry_clock,
                          "mode":"paper","bar_ts":env.bar_open.isoformat(),"decision_at":t.isoformat(),
                          "fired":True})
    add_episode(j, f, i=1440)
    result = worker.cycle(NOW+timedelta(minutes=2))
    assert result["verified_outcomes"] == 1
    assert len(result["groups"]) == 3
    assert all(g["net_usd"] == pytest.approx(1.8) for g in result["groups"])
    assert all(g["state"] == "INSUFFICIENT" for g in result["groups"])
    assert worker.store.read("verified_stage_outcome", s.build_ledger_labels(j.path,f.path)["labels"][0]["label_hash"],now=NOW+timedelta(minutes=3))
    assert not (journals/".dashboard_signal_queue_v1.sqlite").exists()
    # Malformed tail must withdraw results, not serve the previous successful audit.
    with f.path.open("a") as handle:
        handle.write("{broken\n")
    result = worker.cycle(NOW+timedelta(minutes=3))
    assert result["verified_outcomes"] == 0 and not result["groups"]
    assert result["audits"][0]["state"] == "BLOCKED"


def test_stage_endpoint_authenticated_read_only(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from vnedge.dashboard.app import SnapshotProvider, create_app
    monkeypatch.setenv("DASHBOARD_PUBLIC_READ_ONLY","0")
    monkeypatch.setattr(s,"DEFAULT_PATH",tmp_path/"missing.sqlite")
    client = TestClient(create_app(SnapshotProvider(),token="test-stage",journal_dir=tmp_path))
    url = "/api/crypto-analyst/stage-outcomes/BTCUSD"
    headers = {"Authorization":"Bearer test-stage"}
    assert client.get(url).status_code == 401
    response = client.get(url,headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "worker_not_started"
    assert not response.json()["can_trade"]
    assert client.get(url+"?exchange=binanceusdm",headers=headers).status_code == 422
    assert client.post(url,headers=headers,json={"enable":True}).status_code == 405
