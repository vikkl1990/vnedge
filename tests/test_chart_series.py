"""Chart series: canonical source, bounded size, and exactly one marker path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from vnedge.dashboard.chart_series import MAX_BARS, ChartReadError, candles_payload

UTC = timezone.utc


@dataclass
class _C:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str = "canonical_tick_lake"


class _Store:
    def __init__(self, n):
        base = datetime(2026, 8, 1, tzinfo=UTC)
        self.rows = [
            _C(base + timedelta(hours=i), Decimal("100"), Decimal("101"),
               Decimal("99"), Decimal("100.5"), Decimal("7"))
            for i in range(n)
        ]

    def read(self, symbol, timeframe):
        return self.rows


def test_candles_come_from_the_canonical_store_and_say_so() -> None:
    """The UI must not become a fourth candle source."""
    payload = candles_payload(_Store(10), "BTCUSDT", "1h")
    assert payload["source"] == "canonical_lake"
    assert payload["count"] == 10
    assert payload["candles"][0]["time"] == int(
        datetime(2026, 8, 1, tzinfo=UTC).timestamp()
    )


def test_chart_series_sorts_and_deduplicates_recovery_rows() -> None:
    """Recovery/upsert overlap must never reach Lightweight Charts unordered."""
    base = datetime(2026, 8, 1, tzinfo=UTC)
    first = _C(
        base,
        Decimal(100),
        Decimal(101),
        Decimal(99),
        Decimal("100.5"),
        Decimal(7),
    )
    corrected = _C(
        base,
        Decimal(100),
        Decimal(102),
        Decimal(98),
        Decimal("101.5"),
        Decimal(9),
    )
    later = _C(
        base + timedelta(hours=1),
        Decimal("101.5"),
        Decimal(103),
        Decimal(101),
        Decimal(102),
        Decimal(8),
    )

    class _RecoveryStore:
        def read(self, *_):
            return [later, first, corrected]

    with pytest.raises(ChartReadError, match="conflicting_duplicate"):
        candles_payload(_RecoveryStore(), "BTCUSDT", "1h")


def test_the_series_is_bounded() -> None:
    """A year of 1m bars is 525k rows; a browser must never be handed that."""
    payload = candles_payload(_Store(MAX_BARS + 500), "BTCUSDT", "1m", limit=99_999)
    assert payload["count"] == MAX_BARS
    assert payload["truncated"] is True


def test_chart_series_honours_provider_range_in_epoch_milliseconds() -> None:
    start = datetime(2026, 8, 1, 3, tzinfo=UTC)
    end = datetime(2026, 8, 1, 5, tzinfo=UTC)
    payload = candles_payload(
        _Store(10),
        "BTCUSDT",
        "1h",
        limit=500,
        from_ms=int(start.timestamp() * 1_000),
        to_ms=int(end.timestamp() * 1_000),
    )
    assert payload["count"] == 3
    assert payload["candles"][0]["time"] == int(start.timestamp())
    assert payload["candles"][-1]["time"] == int(end.timestamp())


def test_an_unreadable_store_cannot_claim_genesis() -> None:
    class _Broken:
        def read(self, *_):
            raise OSError("lake unavailable")

    with pytest.raises(ChartReadError, match="chart_store_read_failed"):
        candles_payload(_Broken(), "BTCUSDT", "1h")


def test_the_endpoints_are_registered_and_authorised(tmp_path) -> None:
    """The chart is read-only and behind the same auth as everything else."""
    from starlette.testclient import TestClient

    from vnedge.dashboard.app import SnapshotProvider, create_app

    client = TestClient(create_app(SnapshotProvider(), token="t"))
    paths = {r.path for r in client.app.routes if hasattr(r, "path")}
    assert "/api/candles/{symbol}" in paths
    # Markers are built client-side from journal scanner_events. A server-side
    # marker route would be a second path to one answer; its absence is the fix.
    assert "/api/candles/{symbol}/markers" not in paths

    # unauthenticated callers get nothing
    assert client.get("/api/candles/BTCUSDT").status_code in (401, 403)

    ok = client.get("/api/candles/BTCUSDT?token=t")
    assert ok.status_code == 200
    body = ok.json()
    assert body["source"] == "canonical_lake"      # not a fourth feed
    assert isinstance(body["candles"], list)

    assert client.get("/api/candles/BTCUSDT/markers?token=t").status_code == 404


def persisted_store(tmp_path):
    from vnedge.data.candles import Candle, CandleParquetStore
    store = CandleParquetStore(tmp_path, exchange="delta_india")
    opened = datetime(2026, 9, 1, tzinfo=UTC)
    bars = [Candle(symbol="BTCUSD", timeframe="1h", open_time=opened+timedelta(hours=i),
                   close_time=opened+timedelta(hours=i+1), open=Decimal(100), high=Decimal(102),
                   low=Decimal(99), close=Decimal(101), volume=Decimal(1), quote_volume=Decimal(101),
                   trade_count=2, taker_buy_volume=Decimal("0.5"), vwap=Decimal(101)) for i in range(3)]
    store.upsert(bars)
    return store, bars


def test_persisted_proof_is_projected_without_any_write(tmp_path):
    store, bars = persisted_store(tmp_path)
    path = store.partition_path(bars[0])
    before = path.read_bytes()
    result = candles_payload(store, "BTCUSD", "1h")
    assert path.read_bytes() == before
    assert all(b["proof_state"] == "CLOSED" and b["hash_valid"] for b in result["candles"])
    assert result["candles"][0]["source"] == "canonical_tick_lake"
    assert result["revision_cutoff_ms"] == int(bars[-1].open_time.timestamp()*1000)


@pytest.mark.parametrize("field,value", [("is_closed", None), ("is_closed", "false"),
    ("coverage_ok", "true"), ("content_sha256", None), ("content_sha256", "f"*64), ("data_quality", "partial")])
def test_missing_or_bad_proof_is_never_reconstructed(tmp_path, field, value):
    import pandas as pd
    store, bars = persisted_store(tmp_path)
    path = store.partition_path(bars[0])
    frame = pd.read_parquet(path)
    frame[field] = value
    frame.to_parquet(path)
    before = path.read_bytes()
    result = candles_payload(store, "BTCUSD", "1h")
    assert all(b["proof_state"] == "UNVERIFIED" for b in result["candles"])
    assert path.read_bytes() == before


def test_official_and_repaired_rows_cannot_enter_desk_series(tmp_path):
    store, bars = persisted_store(tmp_path)
    store.upsert([bars[0]], source="official_delta_ohlc")
    store.upsert([bars[1]], source="repaired", data_quality="partial", coverage_ok=False)
    result = candles_payload(store, "BTCUSD", "1h")
    assert result["count"] == 1
    assert result["excluded_sources"] == {"official_delta_ohlc": 1, "repaired": 1}


def test_prefix_revision_changes_on_repair_not_append(tmp_path):
    from dataclasses import replace
    store, bars = persisted_store(tmp_path)
    first = candles_payload(store, "BTCUSD", "1h", limit=1)
    store.upsert([replace(bars[-1], open_time=bars[-1].open_time+timedelta(hours=1),
                          close_time=bars[-1].close_time+timedelta(hours=1))])
    appended = candles_payload(store, "BTCUSD", "1h", limit=1,
                               revision_before_ms=first["revision_cutoff_ms"])
    assert appended["previous_revision"] == first["series_revision"]
    assert appended["series_revision"] != first["series_revision"]
    # Repair outside the returned tail still invalidates the prefix fingerprint.
    store.upsert([replace(bars[0], close=Decimal("100.5"))])
    repaired = candles_payload(store, "BTCUSD", "1h", limit=1,
                               revision_before_ms=appended["revision_cutoff_ms"])
    assert repaired["previous_revision"] != appended["series_revision"]


def test_read_error_is_http_503_not_empty_200(monkeypatch):
    from starlette.testclient import TestClient
    from vnedge.dashboard.app import SnapshotProvider, create_app
    import vnedge.dashboard.app as app_module
    def broken(*args, **kwargs):
        raise ChartReadError("chart_store_read_failed")
    monkeypatch.setattr(app_module, "candles_payload", broken)
    client = TestClient(create_app(SnapshotProvider(), token="t"))
    response = client.get("/api/candles/BTCUSD?token=t&exchange=delta_india")
    assert response.status_code == 503
    assert response.json()["status"] == "ERROR"


def test_journal_chart_projection_keeps_validated_arm_identity():
    from vnedge.execution.evidence import DecisionEnvelope, ExecutionEvidence
    from vnedge.strategy.arm_evidence import freeze_permission_from_row
    from vnedge.dashboard.trade_journal import _scanner_audit_events
    snapshot = freeze_permission_from_row(dict(timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        open=100., high=102., low=99., close=101., volume=1., quote_volume=101., trade_count=2,
        is_closed=True, data_quality="ok", candle_source="canonical_tick_lake"),
        decision_timeframe="15m", context_timeframes=(), allow_long=True, allow_short=False, reason="fixture")
    arm = DecisionEnvelope.create(strategy_id="range_expansion_realtime_v2", symbol="BTC/USD:USD",
        timeframe="15m", side="long", permission_snapshot=snapshot, entry_clock="quote_hold")
    payload = {"fired": True, "bar_ts": "2026-09-01T00:15:00+00:00", "signal": {"arm_envelope": arm.as_dict()}}
    event = _scanner_audit_events([("lane", {"kind": "lane_eval", "ts": "2026-09-01T00:15:02Z", "payload": payload})])[0]
    assert event["bar_ts"] == arm.bar_open.isoformat()
    assert event["decision_id"] == arm.decision_id
    assert event["permission_snapshot"]["snapshot_id"] == arm.snapshot_id
    assert event["evidence_bound"]
    nested = {"execution_evidence": ExecutionEvidence.from_decision(arm).as_dict(), "approved": True}
    event = _scanner_audit_events([("lane", {"kind": "shadow_intent", "payload": nested})])[0]
    assert event["evidence_bound"]
    nested["decision_id"] = "different_outer_decision"
    event = _scanner_audit_events([("lane", {"kind": "shadow_intent", "payload": nested})])[0]
    assert not event["evidence_bound"]
    payload["signal"]["arm_envelope"]["decision_id"] = "forged"
    event = _scanner_audit_events([("lane", {"kind": "lane_eval", "payload": payload})])[0]
    assert not event["evidence_bound"]
