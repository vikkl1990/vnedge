"""Immutable research evidence bundles are verifiable and dashboard-readable."""

from __future__ import annotations

import json

from vnedge.dashboard.backtest_lab import load_backtest_lab
from vnedge.research.evidence_bundle import (
    BUNDLE_SCHEMA,
    list_bundle_index,
    publish_backtest_bundle,
    verify_bundle,
)


def _report(data_source: str) -> dict:
    return {
        "schema": "vnedge.backtest_report.v1",
        "run": {
            "run_id": "run-001",
            "status": "COMPLETE",
            "generated_at": "2026-08-28T00:00:00+00:00",
            "engine": "vnedge.backtest.run_backtest",
            "evidence_class": "EXPLORATORY",
            "strategy_id": "trend_continuation_v1",
            "exchange": "binanceusdm",
            "symbol": "BTC/USDT:USDT",
            "timeframe": "1h",
            "data_source": data_source,
            "bars": 100,
            "window": {
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-05T03:00:00+00:00",
            },
            "parameters": {"breakout_bars": 48},
            "costs": {"modeled_taker_round_trip_bps": 17.0},
        },
        "overview": {
            "num_trades": 4,
            "gross_profit_usd": 12.0,
            "net_profit_usd": 4.0,
            "total_cost_usd": 8.0,
            "profit_factor": 1.3,
            "max_drawdown_pct": 2.1,
        },
        "equity_curve": [],
        "daily": [],
        "monthly": [],
        "trades": [],
        "warnings": ["Not sealed OOS evidence"],
        "governance": {"can_trade": False, "can_promote": False, "read_only": True},
    }


def test_bundle_is_content_addressed_idempotent_and_indexed(tmp_path):
    data = tmp_path / "candles.parquet"
    data.write_bytes(b"canonical-candle-bytes")
    root = tmp_path / "bundles"
    index = root / "index.sqlite"

    first = publish_backtest_bundle(
        _report(str(data)),
        root=root,
        index_path=index,
        code_sha="abc123",
        revision_id="trend_continuation_v1@v1",
        engine_version="runner-v2",
        parity_status="EXACT",
    )
    second = publish_backtest_bundle(
        _report(str(data)),
        root=root,
        index_path=index,
        code_sha="abc123",
        revision_id="trend_continuation_v1@v1",
        engine_version="runner-v2",
        parity_status="EXACT",
    )

    assert first.schema_id == BUNDLE_SCHEMA
    assert first.bundle_id == second.bundle_id
    assert verify_bundle(root / first.bundle_id).valid is True
    rows = list_bundle_index(index)
    assert [row["bundle_id"] for row in rows] == [first.bundle_id]
    assert rows[0]["parity_status"] == "EXACT"
    assert rows[0]["net_profit_usd"] == 4.0
    index.chmod(0o444)
    try:
        assert list_bundle_index(index)[0]["bundle_id"] == first.bundle_id
    finally:
        index.chmod(0o644)


def test_bundle_tampering_is_detected(tmp_path):
    root = tmp_path / "bundles"
    manifest = publish_backtest_bundle(_report("missing.parquet"), root=root)
    report_path = root / manifest.bundle_id / "report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["overview"]["net_profit_usd"] = 999999
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    verification = verify_bundle(root / manifest.bundle_id)
    assert verification.valid is False
    assert "report digest mismatch" in verification.errors


def test_bundle_refuses_report_with_trade_or_promotion_authority(tmp_path):
    report = _report("missing.parquet")
    report["governance"]["can_trade"] = True

    try:
        publish_backtest_bundle(report, root=tmp_path / "bundles")
    except ValueError as exc:
        assert "deny trading authority" in str(exc)
    else:  # pragma: no cover - this is the safety invariant under test
        raise AssertionError("unsafe report was published")


def test_backtest_lab_loads_verified_bundle_provenance(tmp_path):
    root = tmp_path / "bundles"
    manifest = publish_backtest_bundle(
        _report("missing.parquet"),
        root=root,
        code_sha="build789",
    )

    payload = load_backtest_lab(
        jobs_dir=tmp_path / "jobs",
        reports_dir=tmp_path / "reports",
        artifact_dir=tmp_path / "artifacts",
        evidence_bundle_dir=root,
        evidence_index_path=root / "index.sqlite",
    )

    assert payload["selected_run_id"] == "run-001"
    assert payload["selected"]["overview"]["net_profit_usd"] == 4.0
    assert payload["selected_evidence"]["bundle_id"] == manifest.bundle_id
    assert payload["selected_evidence"]["governance"]["can_trade"] is False
    assert payload["runs"][0]["code_sha"] == "build789"
