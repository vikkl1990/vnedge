"""Governed Arena experiments. Offline research; no execution authority.

The packet is written before candidate evaluation. The falsifier sees only
the frozen packet and machine results, never a proposer's conclusion. It is a
deterministic audit, not an LLM debate or proof of economic edge.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from vnedge.backtest.backtester import BacktestConfig
from vnedge.plan.cost_model import COST_PROFILES
from vnedge.runtime.latency_tracker import timeframe_to_seconds
from vnedge.strategy.arm_evidence import assert_decision_row


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    strategy_id: str = Field(min_length=4)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim: str = Field(min_length=12, max_length=1000)
    invalidation: str = Field(min_length=12, max_length=1000)
    exchange: Literal["delta_india"] = "delta_india"
    symbol: Literal["BTC/USD:USD", "ETH/USD:USD"] = "BTC/USD:USD"
    timeframe: Literal["5m", "15m", "1h"] = "1h"
    entry_clock: Literal["next_open", "quote_hold", "maker_retest"] = "next_open"
    context_timeframes: tuple[str, ...] = ()
    exact_volume: bool = False
    cost_profile_id: Literal["delta_swing", "delta_scalp"]
    train_bars: int = Field(default=1440, ge=2, le=10000)
    test_bars: int = Field(default=720, ge=2, le=10000)
    max_holding_bars: int = Field(default=48, ge=1, le=192)
    # Explicit limitations, not an account-verified venue specification.
    funding: Literal["excluded"] = "excluded"
    judgment: Literal["exploratory_rolling_only"] = "exploratory_rolling_only"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def persist_once(path: Path, data: bytes) -> None:
    """Exclusive create; a torn/different artifact is an error, never replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError(f"immutable_artifact_conflict:{path.name}") from None


def runtime_identity() -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    package = Path(__file__).resolve().parents[1]
    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True, timeout=5, check=False,
            )
            if result.returncode == 0:
                return result.stdout.decode().strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        # Production images have neither a .git checkout nor a git executable.
        # The build stamps its revision separately; content fingerprint remains
        # mandatory regardless of whether that optional attribution is present.
        for stamp in (root / "BUILD_SHA", Path.cwd() / "BUILD_SHA"):
            if stamp.is_file():
                return stamp.read_text().strip()
        return "unavailable"
    # Includes uncommitted/untracked Python implementations, not only HEAD.
    code = hashlib.sha256()
    for file in sorted(package.rglob("*.py")):
        code.update(str(file.relative_to(package)).encode())
        code.update(file.read_bytes())
    return {"git_commit": git("rev-parse", "HEAD"), "code_sha256": code.hexdigest(),
            "python": platform.python_version(), "pandas": pd.__version__, "numpy": np.__version__}


def preflight(
    spec: ExperimentSpec, candles: pd.DataFrame, *, warmup_bars: int,
    now: datetime, source_sha256: str, strategy_id: str,
) -> dict[str, Any]:
    failures: list[str] = []
    if "timestamp" not in candles.columns:
        failures.append("decision_timestamp_column_missing")
    if spec.source_sha256 != source_sha256 or spec.strategy_id != strategy_id:
        failures.append("candidate_identity_mismatch")
    if spec.entry_clock != "next_open":
        failures.append("lane_bbo_replay_required")
    if spec.context_timeframes:
        failures.append("bound_context_replay_required")
    if warmup_bars < 0 or spec.train_bars <= warmup_bars:
        failures.append("training_warmup_insufficient")
    required = spec.train_bars + spec.test_bars
    if len(candles) < required:
        failures.append("history_insufficient")
    seconds = timeframe_to_seconds(spec.timeframe)
    invalid: dict[str, int] = {}
    previous: datetime | None = None
    for row in candles.to_dict("records"):
        try:
            ref = assert_decision_row(row, timeframe=spec.timeframe)
            if ref.close_time > now:
                raise ValueError("future_bar")
            for key, expected in (("exchange", spec.exchange), ("symbol", spec.symbol),
                                  ("timeframe", spec.timeframe)):
                if row.get(key) != expected:
                    raise ValueError(f"{key}_identity_missing_or_mismatch")
            if row.get("coverage_ok") not in (True, 1):
                raise ValueError("coverage_unproven")
            if previous is not None and (ref.open_time - previous).total_seconds() != seconds:
                raise ValueError("non_consecutive_bars")
            previous = ref.open_time
            volume = float(row.get("volume", float("nan")))
            if not np.isfinite(volume) or volume < 0:
                raise ValueError("base_volume_invalid")
            if spec.exact_volume:
                quote = float(row.get("quote_volume", float("nan")))
                if volume <= 0 or not np.isfinite(quote) or quote <= 0:
                    raise ValueError("exact_volume_window_not_ready")
        except (ValueError, TypeError) as exc:
            code = str(exc)
            invalid[code] = invalid.get(code, 0) + 1
    failures.extend(sorted(invalid))
    return {"status": "READY_TO_TEST" if not failures else "NOT_TESTABLE",
            "as_of": now.isoformat(),
            "failures": failures, "invalid_row_counts": invalid,
            "bars_available": len(candles), "bars_required": required,
            "warmup_bars": warmup_bars}


def freeze_packet(
    root: Path, spec: ExperimentSpec, candles: pd.DataFrame, source: bytes,
    config: BacktestConfig, gates: dict[str, Any], check: dict[str, Any],
    runtime: dict[str, str],
) -> dict[str, Any]:
    # Exact input floats and schema retained; no rounded JSON candle substitute.
    data = candles.to_parquet(index=False)
    data_hash = digest(data)
    persist_once(root / "inputs" / f"{data_hash}.parquet", data)
    persist_once(root / "sources" / f"{digest(source)}.py", source)
    costs = asdict(COST_PROFILES[spec.cost_profile_id])
    fee = config.fees.taker_bps
    payload = {
        "schema_version": 1, "spec": spec.model_dump(mode="json"),
        "dataset_sha256": data_hash, "runtime": runtime,
        "config": config.model_dump(mode="json"), "gates": gates,
        "costs": {**costs, "entry_bps": fee + config.slippage.bps,
                  "exit_bps": fee + config.slippage.bps,
                  "booked_round_bps": 2 * (fee + config.slippage.bps),
                  "fill_assumption": "next_open_taker_bar_exit_stop_first",
                  "funding": "excluded", "venue_limits": "research_assumptions_not_verified"},
        "preflight": check, "benchmark": "flat_cash_zero_net",
        "split": {"train_bars": spec.train_bars, "test_bars": spec.test_bars,
                  "step_bars": spec.test_bars, "untouched_judgment": False,
                  "parameter_grid": [{}]},
        "can_trade": False, "can_promote": False, "performance_eligible": False,
    }
    packet_id = digest(json_bytes(payload))
    packet = {"packet_id": packet_id, **payload}
    persist_once(root / "packets" / f"{packet_id}.json", json_bytes(packet))
    return packet


def falsify(packet: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Separate machine-evidence auditor; no narrative input or vote averaging."""
    agreed = ["packet_frozen_before_evaluation"]
    contested: list[str] = []
    unverified = ["untouched_judgment", "cost_stress_replay", "execution_approval_parity",
                  "funding_costs", "venue_product_limits"]
    wf = result.get("walk_forward") or {}
    causal = result.get("causality") or {}
    if packet["preflight"]["status"] != "READY_TO_TEST":
        status = "NOT_TESTABLE"
        unverified.extend(packet["preflight"]["failures"])
    elif not causal.get("passed"):
        status = "REFUSED"
        contested.append("causality_not_verified")
    elif not wf or int(wf.get("oos_trades", 0)) == 0:
        status = "INSUFFICIENT_EVIDENCE"
        unverified.append("nonzero_oos_trades")
    else:
        agreed.append("truncation_check_passed_on_observed_data")
        if float(wf.get("oos_net_usd", 0)) <= 0:
            contested.append("did_not_beat_flat_cash_after_booked_costs")
        if not wf.get("passed"):
            contested.append("frozen_promotion_gates_failed")
        status = "CHALLENGED" if contested else "MORE_PROOF_REQUIRED"
    return {"kind": "deterministic_falsifier_v1", "packet_id": packet["packet_id"],
            "status": status, "agreed": agreed, "contested": contested,
            "unverified": unverified, "can_trade": False, "can_promote": False}
