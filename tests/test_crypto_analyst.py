from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.crypto_analyst import CryptoAnalystService, analyse_rows, read_window
from vnedge.data.bar_identity import bar_content_sha256
from vnedge.data.candles import _decision_hash_row

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def stamp(row):
    row["content_sha256"] = bar_content_sha256(
        _decision_hash_row(row),
        open_time=row["open_time"],
        close_time=row["close_time"],
        source=row["source"],
    )
    return row


def bars(count=100, downward=False):
    result = []
    for i in range(count):
        price = Decimal(1000 - i if downward else 1000 + i)
        opened = NOW - timedelta(minutes=15 * (count - i))
        result.append(
            stamp(
                {
                    "open_time": opened,
                    "close_time": opened + timedelta(minutes=15),
                    "open": price,
                    "high": price + 2,
                    "low": price - 2,
                    "close": price - 1 if downward else price + 1,
                    "volume": Decimal(10),
                    "quote_volume": price * 10,
                    "trade_count": 10,
                    "source": "canonical_tick_lake",
                    "is_closed": True,
                    "coverage_ok": True,
                    "data_quality": "ok",
                }
            )
        )
    return result


def run(rows, now=NOW):
    return analyse_rows(rows, "BTCUSD", "delta_india", "15m", now)


def test_analysis_supports_both_directions_without_trade_authority():
    long, short = run(bars()), run(bars(downward=True))
    assert long["bias"] == "bullish" and short["bias"] == "bearish"
    assert long["alignment"] > 0 > short["alignment"]
    assert long["can_trade"] is False
    assert long["execution"]["net_edge_bps"] is None
    assert long["conflicts"]  # neutral volume is not counted as support
    assert len(long["analysis_id"]) == 64


def test_asof_future_and_forming_bars_do_not_change_analysis():
    rows = bars()
    baseline = run(rows)
    future = dict(
        rows[-1], open_time=NOW, close_time=NOW + timedelta(minutes=15), close=Decimal(90000)
    )
    assert run(rows + [future]) == baseline
    forming = dict(future, is_closed=False)
    assert run(rows + [forming]) == baseline


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"content_sha256": "0" * 64}, "bar_hash_invalid"),
        ({"source": "official_delta_ohlc"}, "noncanonical_source"),
        ({"coverage_ok": False}, "bar_coverage_unverified"),
        ({"coverage_ok": "true"}, "bar_coverage_unverified"),
        ({"symbol": "ETHUSD"}, "series_identity_mismatch"),
        ({"is_closed": None}, "closed_bar_proof_missing"),
        ({"close": float("nan")}, "ohlcv_invalid"),
    ],
)
def test_bad_latest_row_is_not_replaced_by_previous_good_row(changes, reason):
    rows = bars()
    rows[-1].update(changes)
    report = run(rows)
    assert report["state"] == "unavailable"
    assert reason in report["issues"]
    assert report["alignment"] is None


def test_gap_does_not_get_filled_or_joined_across():
    rows = bars()
    del rows[-20]
    report = run(rows)
    assert report["state"] == "unavailable"
    assert report["bars"] == 19
    assert "history_gap" in report["issues"]
    assert report["history"]["contiguous_bars"] == 19
    assert report["history"]["required_bars"] == 60
    assert report["history"]["status"] == "collecting"
    assert report["metrics"] == {}  # public observations never become technical prices


def test_old_missing_closed_proof_cuts_history_instead_of_poisoning_new_window():
    rows = bars()
    rows[10]["is_closed"] = None
    report = run(rows)
    assert report["state"] == "current"
    assert report["bars"] == 89
    assert "closed_bar_proof_missing" in report["issues"]
    assert report["alignment"] == run(rows[11:])["alignment"]
    assert report["series_hash"] == run(rows[11:])["series_hash"]
    assert rows[10]["is_closed"] is None  # no repair or attestation by the reader


def test_recent_missing_closed_proof_still_blocks_and_reports_progress():
    rows = bars()
    rows[-20]["is_closed"] = None
    report = run(rows)
    assert report["state"] == "unavailable"
    assert report["history"]["contiguous_bars"] == 19
    assert report["history"]["status"] == "collecting"
    assert "closed_bar_proof_missing" in report["issues"]
    assert report["alignment"] is None


def test_missing_latest_proof_has_no_fabricated_history_progress():
    rows = bars()
    rows[-1].pop("is_closed")
    report = run(rows)
    assert report["history"]["status"] == "unverified"
    assert report["history"]["contiguous_bars"] == 0
    assert report["as_of"] is None


def test_old_gap_is_disclosed_and_features_restart_on_contiguous_suffix():
    rows = bars()
    del rows[10]
    report = run(rows)
    assert report["state"] == "current" and report["bars"] == 89
    assert "history_gap" in report["issues"]


def test_duplicate_conflict_and_repair_identity():
    rows = bars()
    duplicate = stamp(dict(rows[-1], volume=Decimal(12)))
    assert run(rows + [duplicate])["issues"] == ["conflicting_duplicate"]
    old = run(rows)
    rows[-1] = duplicate
    assert old["analysis_id"] != run(rows)["analysis_id"]


def test_exact_session_mean_uses_notional_not_close_proxy():
    rows = bars()
    for row in rows:
        row["quote_volume"] = Decimal(10000)
        stamp(row)
    assert run(rows)["metrics"]["session_vwap"] == 1000
    rows[-1]["quote_volume"] = None
    stamp(rows[-1])
    report = run(rows)
    assert report["metrics"]["session_vwap"] is None
    assert "session_vwap_unavailable" in report["issues"]


def test_session_requires_midnight_coverage():
    # Analysis is valid after 60 bars, but a 1h session starting before the
    # window cannot be given a fabricated exact session VWAP.
    rows = bars(60)
    later = NOW + timedelta(hours=10)
    for row in rows:
        row["open_time"] += timedelta(hours=10)
        row["close_time"] += timedelta(hours=10)
        stamp(row)
    report = run(rows, later)
    assert report["state"] == "current"
    assert report["metrics"]["session_vwap"] is None


def test_stale_data_and_missing_volume_remain_explicit():
    rows = bars()
    for row in rows:
        row["volume"] = Decimal(0)
        stamp(row)
    report = run(rows, NOW + timedelta(hours=1))
    assert report["state"] == "stale"
    assert report["metrics"]["volume_ratio"] is None
    assert report["coverage_pct"] == 85
    assert "stale_series" in report["issues"]


def test_breakout_compares_against_prior_high_and_volume_baseline():
    rows = bars()
    rows[-1].update(close=Decimal(1200), high=Decimal(1201), volume=Decimal(30))
    stamp(rows[-1])
    report = run(rows)
    assert report["metrics"]["range_high"] == 1100
    assert report["metrics"]["volume_ratio"] == 3
    assert "upside_breakout" in report["setups"]


def test_empty_lake_is_not_a_fabricated_market(tmp_path):
    report = CryptoAnalystService(tmp_path).snapshot("delta_india", "15m")
    assert report["universe"]["current"] == 0
    assert report["breadth"]["denominator"] == 0
    assert all(r["alignment"] is None for r in report["markets"])
    assert report["can_trade"] is False and report["can_promote"] is False


def test_partition_reader_preserves_proof_and_does_not_write(tmp_path):
    directory = tmp_path / "exchange=delta_india" / "BTCUSD" / "15m"
    directory.mkdir(parents=True)
    path = directory / "2026-09-13.parquet"
    pd.DataFrame(bars()).to_parquet(path)
    before = path.read_bytes()
    report = run(read_window(tmp_path, "delta_india", "BTCUSD", "15m", NOW))
    assert report["state"] == "current"
    assert path.read_bytes() == before


def test_reader_rejects_symlink_partition(tmp_path):
    directory = tmp_path / "exchange=delta_india" / "BTCUSD" / "15m"
    directory.mkdir(parents=True)
    (directory / "2026-09-13.parquet").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="partition_read_bound"):
        read_window(tmp_path, "delta_india", "BTCUSD", "15m", NOW)


def test_endpoint_auth_and_scope_validation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(SnapshotProvider(), token="test"))
    assert client.get("/api/crypto-analyst").status_code == 401
    headers = {"Authorization": "Bearer test"}
    response = client.get("/api/crypto-analyst", headers=headers)
    assert response.status_code == 200 and response.json()["can_trade"] is False
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/crypto-analyst?exchange=../../etc", headers=headers).status_code == 422
    assert client.get("/api/crypto-analyst?timeframe=1s", headers=headers).status_code == 422


def test_cross_asset_comparison_requires_same_clock_and_binds_benchmark(tmp_path, monkeypatch):
    import vnedge.dashboard.crypto_analyst as module

    actual = datetime.now(UTC)
    aligned = actual.replace(minute=actual.minute // 15 * 15, second=0, microsecond=0)
    btc = bars()
    for r in btc:
        r["open_time"] += aligned - NOW
        r["close_time"] += aligned - NOW
        stamp(r)
    eth = [dict(r) for r in btc]
    monkeypatch.setattr(
        module,
        "read_window",
        lambda root, exchange, symbol, tf, now: btc if symbol == "BTCUSD" else eth,
    )
    first = CryptoAnalystService(tmp_path).snapshot("delta_india", "15m")
    first_eth = next(r for r in first["markets"] if r["symbol"] == "ETHUSD")
    assert first["breadth"]["denominator"] == 2
    assert first_eth["metrics"]["relative_btc_12_pct"] == 0
    assert first_eth["benchmark_ref"]["series_hash"]
    btc[-1] = stamp(dict(btc[-1], volume=Decimal(11)))
    second = CryptoAnalystService(tmp_path).snapshot("delta_india", "15m")
    second_eth = next(r for r in second["markets"] if r["symbol"] == "ETHUSD")
    assert second_eth["analysis_id"] != first_eth["analysis_id"]
    eth.pop()
    third = CryptoAnalystService(tmp_path).snapshot("delta_india", "15m")
    third_eth = next(r for r in third["markets"] if r["symbol"] == "ETHUSD")
    assert third_eth["metrics"]["relative_btc_12_pct"] is None
    assert third["breadth"]["denominator"] == 1
