"""Chart series: canonical source, bounded size, honest markers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from vnedge.dashboard.chart_series import (
    MAX_BARS,
    candles_payload,
    markers_from_journal,
    markers_payload,
)

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


def _outcome(**over):
    payload = {"symbol": "BTCUSDT", "side": "long",
               "entry_bar_ts": "2026-08-19T15:00:00+00:00",
               "bar_ts": "2026-08-19T18:00:00+00:00",
               "resolution": "stop", "virtual_net_usd": -5.31}
    payload.update(over)
    return json.dumps({"kind": "shadow_outcome", "payload": payload})


def test_candles_come_from_the_canonical_store_and_say_so() -> None:
    """The UI must not become a fourth candle source."""
    payload = candles_payload(_Store(10), "BTCUSDT", "1h")
    assert payload["source"] == "canonical_lake"
    assert payload["count"] == 10
    assert payload["candles"][0]["time"] == int(
        datetime(2026, 8, 1, tzinfo=UTC).timestamp()
    )


def test_the_series_is_bounded() -> None:
    """A year of 1m bars is 525k rows; a browser must never be handed that."""
    payload = candles_payload(_Store(MAX_BARS + 500), "BTCUSDT", "1m", limit=99_999)
    assert payload["count"] == MAX_BARS
    assert payload["truncated"] is True


def test_an_unreadable_store_yields_an_empty_series_not_a_crash() -> None:
    class _Broken:
        def read(self, *_):
            raise OSError("lake unavailable")

    payload = candles_payload(_Broken(), "BTCUSDT", "1h")
    assert payload["candles"] == [] and payload["count"] == 0


def test_each_outcome_produces_an_entry_and_an_exit_marker() -> None:
    markers = markers_from_journal([_outcome()], "BTCUSDT")
    assert [m.shape for m in markers] == ["arrowUp", "circle"]
    assert markers[0].time < markers[1].time


def test_exit_colour_encodes_the_outcome() -> None:
    """A red exit under a green entry is the shape a reader scans for."""
    loser = markers_from_journal([_outcome()], "BTCUSDT")[1]
    winner = markers_from_journal([_outcome(virtual_net_usd=12.0)], "BTCUSDT")[1]
    assert loser.color != winner.color
    assert "stop" in loser.text and "-5.31" in loser.text


def test_a_short_is_marked_on_the_opposite_side() -> None:
    short = markers_from_journal([_outcome(side="short")], "BTCUSDT")
    assert short[0].position == "aboveBar" and short[0].shape == "arrowDown"


def test_other_symbols_and_other_record_kinds_are_ignored() -> None:
    lines = [_outcome(symbol="ETHUSDT"),
             json.dumps({"kind": "lane_eval", "payload": {"symbol": "BTCUSDT"}}),
             "not json at all", ""]
    assert markers_from_journal(lines, "BTCUSDT") == []


def test_a_missing_journal_directory_is_empty_not_an_error() -> None:
    from pathlib import Path

    payload = markers_payload(Path("/nonexistent/journals"), "BTCUSDT")
    assert payload["markers"] == [] and payload["journals"] == 0
    assert markers_payload(None, "BTCUSDT")["count"] == 0


def test_markers_are_ordered_and_bounded(tmp_path) -> None:
    path = tmp_path / "lane.journal.jsonl"
    path.write_text("\n".join(
        _outcome(entry_bar_ts=f"2026-08-{d:02d}T15:00:00+00:00",
                 bar_ts=f"2026-08-{d:02d}T18:00:00+00:00")
        for d in range(1, 21)
    ))
    payload = markers_payload(tmp_path, "BTCUSDT", limit=6)
    times = [m["time"] for m in payload["markers"]]
    assert len(times) == 6
    assert times == sorted(times)


def test_the_endpoints_are_registered_and_authorised(tmp_path) -> None:
    """The chart is read-only and behind the same auth as everything else."""
    from starlette.testclient import TestClient

    from vnedge.dashboard.app import SnapshotProvider, create_app

    client = TestClient(create_app(SnapshotProvider(), token="t"))
    paths = {r.path for r in client.app.routes if hasattr(r, "path")}
    assert "/api/candles/{symbol}" in paths
    assert "/api/candles/{symbol}/markers" in paths

    # unauthenticated callers get nothing
    assert client.get("/api/candles/BTCUSDT").status_code in (401, 403)

    ok = client.get("/api/candles/BTCUSDT?token=t")
    assert ok.status_code == 200
    body = ok.json()
    assert body["source"] == "canonical_lake"      # not a fourth feed
    assert isinstance(body["candles"], list)

    markers = client.get("/api/candles/BTCUSDT/markers?token=t")
    assert markers.status_code == 200
    assert isinstance(markers.json()["markers"], list)
