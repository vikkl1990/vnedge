from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vnedge.dashboard.analyst_store import AnalystStore
from vnedge.dashboard.analyst_worker import record_reports
from vnedge.dashboard.analyst_workspace import AnalystWorkspace
from vnedge.dashboard.crypto_fundamentals import (
    collect_fundamentals,
    fundamentals_dossier,
    normalize_metric,
)
from vnedge.dashboard.market_stage import SPEC_HASH, analyse_stage, stage_rows
from vnedge.data.bar_identity import bar_content_sha256
from vnedge.data.candles import _decision_hash_row

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def bars(prices, tf="4h"):
    step = timedelta(hours=4 if tf == "4h" else 24)
    rows = []
    for i, price in enumerate(prices):
        opened = NOW - step * (len(prices) - i)
        row = {
            "open_time": opened,
            "close_time": opened + step,
            "open": Decimal(price),
            "high": Decimal(price) + 2,
            "low": Decimal(price) - 2,
            "close": Decimal(price),
            "volume": Decimal(10),
            "quote_volume": Decimal(price) * 10,
            "trade_count": 10,
            "source": "canonical_tick_lake",
            "data_quality": "ok",
            "coverage_ok": True,
            "is_closed": True,
        }
        row["content_sha256"] = bar_content_sha256(
            _decision_hash_row(row),
            open_time=opened,
            close_time=opened + step,
            source=row["source"],
        )
        rows.append(row)
    return rows


def run(rows, now=NOW, tf="4h"):
    return analyse_stage(rows, "delta_india", "BTCUSD", tf, now)


@pytest.mark.parametrize("tf", ["4h", "1d"])
def test_same_sideways_shape_remembers_opposite_prior_trends(tf):
    base = run(bars(list(range(400, 200, -2)) + [200] * 150, tf), tf=tf)
    top = run(bars(list(range(200, 400, 2)) + [400] * 150, tf), tf=tf)
    assert base["stage"] == "base_after_decline"
    assert top["stage"] == "range_after_advance"
    assert base["prior_direction"] == "down" and top["prior_direction"] == "up"
    assert base["memory_left_censored"] and base["spec_hash"] == SPEC_HASH
    assert not base["can_trade"] and not base["can_promote"]
    assert len(base["stage_id"]) == 64 and base["transitions"]


def test_no_prior_trend_is_unknown_not_accumulation():
    result = run(bars([200] * 100))
    assert result["stage"] == "unknown"
    assert result["prior_direction"] is None


def test_causal_stage_events_future_rows_and_prefix_replay():
    rows = bars(list(range(200, 400)) + [400] * 120)
    cutoff = rows[150]["close_time"]
    prefix = run(rows[:151], now=cutoff)
    assert prefix == run(rows, now=cutoff)
    later = run(rows)
    assert [r for r in later["transitions"] if r["at"] <= cutoff.isoformat()] == prefix[
        "transitions"
    ]
    first = prefix["transitions"][0]
    # First computable close index 59; confirmation is index 60, never backdated.
    assert first["at"] == rows[60]["close_time"].isoformat()


@pytest.mark.parametrize(
    "change",
    [
        {"content_sha256": "bad"},
        {"coverage_ok": False},
        {"source": "official_delta_ohlc"},
        {"symbol": "ETHUSD"},
        {"is_closed": None},
        {"close": float("nan")},
    ],
)
def test_invalid_latest_stage_proof_never_carries_previous(change):
    rows = bars(range(200, 300))
    rows[-1].update(change)
    result = run(rows)
    assert result["state"] == "unavailable" and result["stage_id"] is None


def test_gap_resets_context_instead_of_carrying_old_direction():
    rows = bars(list(range(400, 200, -2)) + [200] * 150)
    del rows[99]
    result = run(rows)
    assert result["stage"] == "unknown" and result["prior_direction"] is None
    assert "stage_history_gap" in result["issues"]


def test_stale_stage_is_historical_and_daily_reader_uses_monthly_files(tmp_path):
    import pandas as pd

    rows = bars(range(200, 300), "1d")
    directory = tmp_path / "exchange=delta_india" / "BTCUSD" / "1d"
    directory.mkdir(parents=True)
    df = pd.DataFrame(rows)
    for month, frame in df.groupby(df.open_time.map(lambda t: t.strftime("%Y-%m"))):
        frame.to_parquet(directory / f"{month}.parquet", index=False)
    read = stage_rows(tmp_path, "delta_india", "BTCUSD", "1d", NOW)
    assert len(read) == 100
    assert run(read, now=NOW + timedelta(days=2), tf="1d")["state"] == "stale"


def payload(slug="ethereum", value=123):
    return {
        "id": "chain#" + slug,
        "slug": slug,
        "protocolType": "chain",
        "totalDataChart": [
            [int((NOW - timedelta(days=1)).timestamp()), value],
            [int(NOW.timestamp()), 999999],
        ],
        "methodology": {"Fees": "User-paid transaction fees"},
    }


def test_fundamentals_exclude_today_and_do_not_backdate_availability(tmp_path):
    writer = AnalystStore(tmp_path / "evidence.sqlite", writable=True)
    body = normalize_metric(payload(value=0), "ETH", "user_fees_usd", NOW)
    ref = writer.append("fundamental", "ETH/user_fees_usd", body, NOW)
    result = fundamentals_dossier(writer, "ETHUSD", NOW)
    fees = result["fields"][0]
    assert fees["value"] == 0 and fees["status"] == "current" and fees["evidence_id"] == ref
    assert not body["historical_backtest_eligible"]
    old = fundamentals_dossier(writer, "ETHUSD", NOW - timedelta(seconds=1))
    assert old["fields"][0]["status"] == "missing"
    stale = fundamentals_dossier(writer, "ETHUSD", NOW + timedelta(days=4))
    assert stale["fields"][0]["status"] == "stale" and stale["fields"][0]["value"] is None
    assert (
        next(f for f in result["fields"] if f["metric"] == "retained_protocol_revenue_usd")[
            "status"
        ]
        == "missing"
    )


def test_btc_applicability_and_unmapped_tokens_are_not_zero_scored(tmp_path):
    store = AnalystStore(tmp_path / "absent.sqlite")
    btc = fundamentals_dossier(store, "BTCUSD", NOW)
    assert (
        next(f for f in btc["fields"] if f["metric"] == "corporate_earnings")["status"]
        == "not_applicable"
    )
    assert btc["health"] == "not_assessed" and btc["fields"][0]["status"] == "missing"
    assert fundamentals_dossier(store, "UNKNOWNUSD", NOW)["template"] == "unmapped_asset"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(id="chain#bitcoin"),
        lambda p: p["totalDataChart"].append([int((NOW - timedelta(days=1)).timestamp()), 77]),
        lambda p: p["totalDataChart"].__setitem__(
            0, [int((NOW - timedelta(days=1)).timestamp()), float("nan")]
        ),
    ],
)
def test_fundamental_invalid_identity_or_value_refused(mutate):
    raw = payload()
    mutate(raw)
    with pytest.raises(ValueError):
        normalize_metric(raw, "ETH", "user_fees_usd", NOW)


def test_collector_persists_sources_and_bounds_repeated_calls(tmp_path, monkeypatch):
    from vnedge.dashboard import crypto_fundamentals as module

    calls = []

    def fake(slug, datatype):
        calls.append((slug, datatype))
        return payload(slug)

    monkeypatch.setattr(module, "fetch_metric", fake)
    store = AnalystStore(tmp_path / "evidence.sqlite", writable=True)
    assert collect_fundamentals(store)["observations"] == 7
    assert collect_fundamentals(store)["status"] == "not_due"
    assert len(calls) == 7


def test_workspace_exposes_separate_context_without_changing_technical_scores(tmp_path):
    workspace = AnalystWorkspace(tmp_path / "candles", tmp_path / "evidence.sqlite")
    dossier = workspace.dossier("delta_india", "BTCUSD")
    assert [s["timeframe"] for s in dossier["stages"]] == ["4h", "1d"]
    assert all(f["alignment"] is None for f in dossier["frames"])
    answer = workspace.answer("delta_india", "BTCUSD", "What stage is this?")
    assert any("no current stage conclusion" in row["text"] for row in answer["answer"])
    answer = workspace.answer("delta_india", "BTCUSD", "fundamentals and earnings")
    assert any("not_applicable" in row["text"] for row in answer["answer"])


def test_stage_memory_is_saved_even_without_intraday_analysis_and_deduplicates(
    tmp_path, monkeypatch
):
    store = AnalystStore(tmp_path / "evidence.sqlite", writable=True)
    workspace = AnalystWorkspace(tmp_path / "candles", store.path)
    stage = run(bars(range(200, 300)))
    monkeypatch.setattr(
        workspace.core,
        "snapshot",
        lambda ex, tf: {"markets": [{"symbol": "BTCUSD"}] if ex == "delta_india" else []},
    )
    monkeypatch.setattr(workspace, "stages", lambda ex, sym, now: [stage])
    assert record_reports(workspace, store) == {"reports": 0, "changes": 0}
    record_reports(workspace, store)
    history = workspace.history("delta_india", "BTCUSD")
    assert history["status"] == "recorded" and len(history["stage_reports"]) == 1
    assert history["stage_reports"][0]["body"]["stages"][0]["stage_id"] == stage["stage_id"]
