"""Agent Gateway job runner stays research-only while producing evidence."""

from __future__ import annotations

import json

import pandas as pd

from vnedge.agent_gateway.job_runner import JobOutcome, run_pending_jobs
from vnedge.agent_gateway.jobs import (
    BLOCKED_STATUS,
    DONE_STATUS,
    FAILED_STATUS,
    PENDING_STATUS,
    create_backtest_job,
    read_job,
)
from vnedge.data.parquet_store import ParquetStore


def _request(**overrides):
    return {
        "strategy_id": "trend_continuation_v1",
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "1h",
        "initial_capital_usd": 500.0,
        "commission_bps": None,
        "slippage_bps": None,
        "strict_mode": True,
        "live_orders_enabled": False,
        "parameters": {"breakout_bars": 5, "max_holding_bars": 8},
        **overrides,
    }


def _candles(rows: int = 340) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=rows, freq="h", tz="UTC")
    close = pd.Series(range(rows), dtype=float) * 0.75 + 100.0
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close - 0.2,
            "high": close + 0.8,
            "low": close - 0.8,
            "close": close,
            "volume": 1000.0,
        }
    )


def test_runner_claims_pending_job_and_persists_terminal_result(tmp_path):
    jobs_dir = tmp_path / "jobs"
    artifact_dir = tmp_path / "artifacts"
    job = create_backtest_job(jobs_dir=jobs_dir, agent="agent", request=_request())

    completed = run_pending_jobs(
        jobs_dir=jobs_dir,
        artifact_dir=artifact_dir,
        executor=lambda _: JobOutcome(
            status=DONE_STATUS,
            result={"runner": "test", "value": 1},
        ),
    )

    assert len(completed) == 1
    stored = read_job(jobs_dir, job["job_id"])
    assert stored is not None
    assert stored["status"] == DONE_STATUS
    assert stored["result"]["value"] == 1
    assert stored["result"]["can_trade"] is False
    assert stored["result"]["can_promote"] is False
    assert stored["result"]["live_orders_enabled"] is False
    assert (artifact_dir / f"{job['job_id']}.json").exists()
    history = [json.loads(line) for line in (jobs_dir / "jobs.jsonl").read_text().splitlines()]
    assert [event["status"] for event in history] == [
        PENDING_STATUS,
        "RUNNING_RESEARCH_ONLY",
        DONE_STATUS,
    ]


def test_runner_records_executor_failure_without_reopening_trading(tmp_path):
    jobs_dir = tmp_path / "jobs"
    job = create_backtest_job(jobs_dir=jobs_dir, agent="agent", request=_request())

    def explode(_job):
        raise RuntimeError("boom")

    completed = run_pending_jobs(jobs_dir=jobs_dir, artifact_dir=None, executor=explode)

    assert completed[0]["job_id"] == job["job_id"]
    assert completed[0]["status"] == FAILED_STATUS
    assert completed[0]["can_trade"] is False
    assert "boom" in completed[0]["error"]


def test_registered_strategy_job_backtests_local_parquet_data(tmp_path):
    data_root = tmp_path / "data"
    jobs_dir = tmp_path / "jobs"
    store = ParquetStore(data_root)
    store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", _candles())
    request = _request(parameters={"breakout_bars": 5, "max_holding_bars": 8, "note": "ignored"})
    job = create_backtest_job(jobs_dir=jobs_dir, agent="agent", request=request)

    completed = run_pending_jobs(
        jobs_dir=jobs_dir,
        data_root=data_root,
        artifact_dir=tmp_path / "artifacts",
    )

    assert completed[0]["job_id"] == job["job_id"]
    assert completed[0]["status"] == DONE_STATUS
    result = completed[0]["result"]
    assert result["execution"] == "registered_strategy_backtest"
    assert result["bars"] == 340
    assert result["accepted_parameters"] == {"breakout_bars": 5}
    assert result["ignored_parameters"] == ["note"]
    assert result["metrics"]["num_trades"] >= 0
    assert result["promotion_verdict"] == "NOT_EVALUATED_AGENT_JOB"
    assert result["can_trade"] is False
    assert result["can_promote"] is False


def test_runner_blocks_missing_data_and_live_enabled_requests(tmp_path):
    jobs_dir = tmp_path / "jobs"
    missing = create_backtest_job(jobs_dir=jobs_dir, agent="agent", request=_request())
    live = create_backtest_job(
        jobs_dir=jobs_dir,
        agent="agent",
        request=_request(live_orders_enabled=True),
    )

    completed = run_pending_jobs(
        jobs_dir=jobs_dir,
        data_root=tmp_path / "data",
        artifact_dir=None,
        max_jobs=2,
    )

    by_id = {job["job_id"]: job for job in completed}
    assert by_id[missing["job_id"]]["status"] == BLOCKED_STATUS
    assert "market data unavailable" in by_id[missing["job_id"]]["blocked_reason"]
    assert by_id[live["job_id"]]["status"] == BLOCKED_STATUS
    assert "live_orders_enabled" in by_id[live["job_id"]]["blocked_reason"]
    assert by_id[live["job_id"]]["can_trade"] is False
