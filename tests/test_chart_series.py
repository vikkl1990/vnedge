"""Chart series: canonical source, bounded size, and exactly one marker path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from vnedge.dashboard.chart_series import MAX_BARS, candles_payload

UTC = timezone.utc


@dataclass
class _C:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


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

    payload = candles_payload(_RecoveryStore(), "BTCUSDT", "1h")
    assert [row["time"] for row in payload["candles"]] == sorted(
        row["time"] for row in payload["candles"]
    )
    assert payload["count"] == 2
    assert payload["candles"][0]["close"] == 101.5


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


def test_an_unreadable_store_yields_an_empty_series_not_a_crash() -> None:
    class _Broken:
        def read(self, *_):
            raise OSError("lake unavailable")

    payload = candles_payload(_Broken(), "BTCUSDT", "1h")
    assert payload["candles"] == [] and payload["count"] == 0


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
