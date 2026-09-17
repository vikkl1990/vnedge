import json
from datetime import UTC, datetime

from vnedge.exchange.delta_capture_health import capture_health, save_capture


def test_missing_status_fails_closed(tmp_path):
    assert not capture_health(tmp_path)["recording"]


def test_every_symbol_required_and_restart_does_not_clear_gaps(tmp_path):
    now = datetime.now(UTC).timestamp()
    save_capture(tmp_path, ["BTCUSD", "ETHUSD"], connected=True,
                 last_trades={"BTCUSD": int(now * 1000), "ETHUSD": int((now - 100) * 1000)})
    assert not capture_health(tmp_path, now=now + 1)["recording"]
    save_capture(tmp_path, ["BTCUSD", "ETHUSD"], connected=True,
                 last_trades={s: int(now * 1000) for s in ("BTCUSD", "ETHUSD")})
    (tmp_path / "reports/delta_recovery_plan.json").write_text(json.dumps({
        "generated_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "symbols": {s: {"status": "GAPS_REMAIN"} for s in ("BTCUSD", "ETHUSD")}}))
    report = capture_health(tmp_path, now=now + 1)
    assert report["recording"]
    assert report["coverage"] == "GAPS_OR_ERRORS"
    assert report["scanner_ready"] is None
    assert not capture_health(tmp_path, now=now + 30)["recording"]


def test_disconnect_and_future_timestamps_fail_closed(tmp_path):
    now = datetime.now(UTC).timestamp()
    for connected, stamp in ((False, now), (True, now + 100)):
        save_capture(tmp_path, ["BTCUSD", "ETHUSD"], connected=connected,
                     last_trades={s: int(stamp * 1000) for s in ("BTCUSD", "ETHUSD")})
        assert not capture_health(tmp_path, now=now + 1)["recording"]


def test_corrupt_status(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports/delta_capture.json").write_text('{"updated_at": null}')
    assert not capture_health(tmp_path)["recording"]


def test_disconnect_requests_repair_even_when_status_disk_fails(tmp_path, monkeypatch):
    import vnedge.exchange.delta_capture_health as health
    from vnedge.exchange.tick_recorder import DeltaTickRecorder
    recorder = DeltaTickRecorder(["BTC/USD:USD", "ETH/USD:USD"], tmp_path)
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(health, "save_capture", fail)
    recorder._on_trade_connection_state(False, datetime.now(UTC))
    assert recorder.recovery_requested.is_set()
    assert recorder._capture_connected is False
