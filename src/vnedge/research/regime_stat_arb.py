"""Causal, regime-aware pairs research for Delta India perpetuals.

This module is deliberately research-only.  It tests the economic claim behind
regime-aware statistical arbitrage without granting a strategy registration or
order authority:

* fit the hedge ratio and a two-state Gaussian HMM on a trailing training set;
* require an Engle-Granger residual ADF statistic to clear a frozen threshold;
* filter (never smooth) the OOS regime probability one bar at a time;
* decide on closed bars and fill at the next bar open;
* keep booked execution cost separate from the conservative CostGate wall.

Funding is not inferred from candles.  Results therefore remain ineligible for
promotion even when net execution PnL is positive.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vnedge.data.delta_native_history import DELTA_INDIA_API_URL
from vnedge.exchange.delta_limit_state import DeltaRestBudget
from vnedge.exchange.delta_ws import delta_native_symbol

_RESOLUTION_SECONDS = {"15m": 900, "1h": 3_600, "4h": 14_400, "1d": 86_400}
_MAX_BARS_PER_PAGE = 1_400
_EPS = 1e-12


@dataclass(frozen=True, slots=True)
class GaussianHMM2:
    initial: tuple[float, float]
    transition: tuple[tuple[float, float], tuple[float, float]]
    means: tuple[float, float]
    variances: tuple[float, float]


@dataclass(frozen=True, slots=True)
class PairModel:
    alpha: float
    beta: float
    adf_t: float
    hmm: GaussianHMM2
    last_probability: tuple[float, float]


@dataclass(frozen=True, slots=True)
class RegimeStatArbConfig:
    train_bars: int = 24 * 90
    test_bars: int = 24 * 14
    adf_critical: float = -3.34
    regime_probability: float = 0.65
    exit_probability: float = 0.55
    entry_z: float = 1.645
    stop_z: float = 3.0
    max_hold_bars: int = 24 * 7
    min_net_edge_bps: float = 4.0
    execution_cost_bps: float = 15.8
    gate_cost_bps: float = 18.8


@dataclass(frozen=True, slots=True)
class PairTrade:
    fold: int
    entry_time: str
    exit_time: str
    side: str
    entry_state: int
    entry_probability: float
    entry_z: float
    expected_edge_bps: float
    hold_bars: int
    exit_reason: str
    gross_bps: float
    execution_cost_bps: float
    gate_cost_bps: float
    net_execution_bps: float
    net_gate_bps: float


@dataclass(frozen=True, slots=True)
class FoldDiagnostic:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    beta: float
    adf_t: float
    adf_critical: float
    cointegrated: bool
    regime_means: tuple[float, float]
    regime_sigmas: tuple[float, float]
    transition: tuple[tuple[float, float], tuple[float, float]]


@dataclass(frozen=True, slots=True)
class RegimeStatArbResult:
    strategy_id: str
    symbols: tuple[str, str]
    timeframe: str
    start: str
    end: str
    bars: int
    folds: int
    cointegrated_folds: int
    fold_diagnostics: tuple[FoldDiagnostic, ...]
    trades: tuple[PairTrade, ...]
    gross_bps: float
    execution_cost_bps: float
    gate_cost_bps: float
    net_execution_bps: float
    net_gate_bps: float
    win_rate: float | None
    profit_factor: float | None
    max_drawdown_bps: float
    performance_eligible: bool
    funding_included: bool
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normal_density(values: np.ndarray, means: np.ndarray, variances: np.ndarray) -> np.ndarray:
    var = np.maximum(variances, 1e-8)
    delta = values[:, None] - means[None, :]
    density = np.exp(-0.5 * delta * delta / var[None, :]) / np.sqrt(
        2.0 * math.pi * var[None, :]
    )
    return np.maximum(density, _EPS)


def _forward(
    values: np.ndarray,
    initial: np.ndarray,
    transition: np.ndarray,
    means: np.ndarray,
    variances: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    emission = _normal_density(values, means, variances)
    alpha = np.empty((len(values), 2), dtype=float)
    scales = np.empty(len(values), dtype=float)
    alpha[0] = initial * emission[0]
    scales[0] = max(float(alpha[0].sum()), _EPS)
    alpha[0] /= scales[0]
    for idx in range(1, len(values)):
        alpha[idx] = (alpha[idx - 1] @ transition) * emission[idx]
        scales[idx] = max(float(alpha[idx].sum()), _EPS)
        alpha[idx] /= scales[idx]
    return alpha, scales, emission


def fit_gaussian_hmm2(values: np.ndarray, *, max_iter: int = 100) -> GaussianHMM2:
    """Fit a small two-state Gaussian HMM with scaled Baum-Welch EM."""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 50:
        raise ValueError("two-state HMM requires at least 50 finite observations")
    variance = max(float(np.var(x)), 1e-6)
    means = np.asarray(np.quantile(x, [0.30, 0.70]), dtype=float)
    variances = np.asarray([variance, variance], dtype=float)
    transition = np.asarray([[0.97, 0.03], [0.03, 0.97]], dtype=float)
    initial = np.asarray([0.5, 0.5], dtype=float)

    for _ in range(max_iter):
        old = np.concatenate((means.copy(), variances.copy(), transition.ravel()))
        alpha, scales, emission = _forward(x, initial, transition, means, variances)
        beta = np.ones_like(alpha)
        for idx in range(len(x) - 2, -1, -1):
            beta[idx] = transition @ (emission[idx + 1] * beta[idx + 1])
            beta[idx] /= max(scales[idx + 1], _EPS)
        gamma = alpha * beta
        gamma /= np.maximum(gamma.sum(axis=1, keepdims=True), _EPS)

        xi_sum = np.zeros((2, 2), dtype=float)
        for idx in range(len(x) - 1):
            xi = (
                alpha[idx, :, None]
                * transition
                * (emission[idx + 1] * beta[idx + 1])[None, :]
            )
            xi_sum += xi / max(float(xi.sum()), _EPS)

        initial = gamma[0]
        transition = xi_sum / np.maximum(gamma[:-1].sum(axis=0)[:, None], _EPS)
        transition /= np.maximum(transition.sum(axis=1, keepdims=True), _EPS)
        weights = np.maximum(gamma.sum(axis=0), _EPS)
        means = (gamma * x[:, None]).sum(axis=0) / weights
        variances = (gamma * (x[:, None] - means[None, :]) ** 2).sum(axis=0) / weights
        variances = np.maximum(variances, 1e-8)
        new = np.concatenate((means, variances, transition.ravel()))
        if float(np.max(np.abs(new - old))) < 1e-8:
            break

    order = np.argsort(means)
    means = means[order]
    variances = variances[order]
    initial = initial[order]
    transition = transition[np.ix_(order, order)]
    return GaussianHMM2(
        initial=(float(initial[0]), float(initial[1])),
        transition=(
            (float(transition[0, 0]), float(transition[0, 1])),
            (float(transition[1, 0]), float(transition[1, 1])),
        ),
        means=(float(means[0]), float(means[1])),
        variances=(float(variances[0]), float(variances[1])),
    )


def filter_probability(
    value: float,
    previous: tuple[float, float],
    hmm: GaussianHMM2,
) -> tuple[float, float]:
    """One causal Hamilton-filter update (no future-data smoothing)."""
    transition = np.asarray(hmm.transition, dtype=float)
    predicted = np.asarray(previous, dtype=float) @ transition
    emission = _normal_density(
        np.asarray([value]),
        np.asarray(hmm.means),
        np.asarray(hmm.variances),
    )[0]
    posterior = predicted * emission
    posterior /= max(float(posterior.sum()), _EPS)
    return float(posterior[0]), float(posterior[1])


def engle_granger_adf_t(residual: np.ndarray, *, lag: int = 1) -> float:
    """ADF t-statistic for a cointegrating residual (critical value is external).

    The regression is ``Δe_t = rho*e_(t-1) + gamma*Δe_(t-1) + error``.
    Residuals from the first-stage OLS are already centered, so no intercept or
    trend is added here.  The configured threshold is deliberately exposed in
    every artifact instead of pretending this statistic is a p-value.
    """
    e = np.asarray(residual, dtype=float)
    e = e[np.isfinite(e)]
    if lag != 1:
        raise ValueError("only the preregistered one-lag ADF is supported")
    if len(e) < 50:
        raise ValueError("ADF requires at least 50 observations")
    delta = np.diff(e)
    y = delta[1:]
    design = np.column_stack((e[1:-1], delta[:-1]))
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    error = y - design @ coef
    dof = len(y) - design.shape[1]
    sigma2 = float(error @ error) / max(dof, 1)
    covariance = sigma2 * np.linalg.pinv(design.T @ design)
    se = math.sqrt(max(float(covariance[0, 0]), _EPS))
    return float(coef[0] / se)


def fit_pair_model(asset_a: np.ndarray, asset_b: np.ndarray) -> PairModel:
    log_a = np.log(np.asarray(asset_a, dtype=float))
    log_b = np.log(np.asarray(asset_b, dtype=float))
    if len(log_a) != len(log_b) or len(log_a) < 100:
        raise ValueError("pair model requires aligned arrays with at least 100 bars")
    design = np.column_stack((np.ones(len(log_b)), log_b))
    alpha, beta = np.linalg.lstsq(design, log_a, rcond=None)[0]
    if not math.isfinite(beta) or beta <= 0:
        raise ValueError("pair hedge ratio must be positive and finite")
    spread = log_a - (alpha + beta * log_b)
    hmm = fit_gaussian_hmm2(spread)
    probability = (hmm.initial[0], hmm.initial[1])
    for value in spread:
        probability = filter_probability(float(value), probability, hmm)
    return PairModel(
        alpha=float(alpha),
        beta=float(beta),
        adf_t=engle_granger_adf_t(spread),
        hmm=hmm,
        last_probability=probability,
    )


def align_pair(asset_a: pd.DataFrame, asset_b: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "open", "close"}
    if not required.issubset(asset_a.columns) or not required.issubset(asset_b.columns):
        raise ValueError("pair frames require timestamp/open/close")
    left = asset_a[list(required)].copy()
    right = asset_b[list(required)].copy()
    for frame in (left, right):
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame.dropna(subset=["timestamp"], inplace=True)
        frame.drop_duplicates("timestamp", keep="last", inplace=True)
    out = left.merge(right, on="timestamp", suffixes=("_a", "_b"), validate="one_to_one")
    out.sort_values("timestamp", inplace=True)
    numeric = ["open_a", "close_a", "open_b", "close_b"]
    out[numeric] = out[numeric].apply(pd.to_numeric, errors="coerce")
    out = out.dropna(subset=numeric)
    out = out[(out[numeric] > 0).all(axis=1)].reset_index(drop=True)
    if out.empty:
        raise ValueError("pair has no aligned positive bars")
    return out


def _trade_return_bps(
    *, side: int, beta: float, entry_a: float, entry_b: float, exit_a: float, exit_b: float
) -> float:
    spread_return = math.log(exit_a / entry_a) - beta * math.log(exit_b / entry_b)
    return side * spread_return / (1.0 + abs(beta)) * 10_000.0


def run_regime_stat_arb(
    asset_a: pd.DataFrame,
    asset_b: pd.DataFrame,
    *,
    symbols: tuple[str, str] = ("BTCUSD", "ETHUSD"),
    timeframe: str = "1h",
    config: RegimeStatArbConfig | None = None,
) -> RegimeStatArbResult:
    cfg = config or RegimeStatArbConfig()
    bars = align_pair(asset_a, asset_b)
    trades: list[PairTrade] = []
    fold_diagnostics: list[FoldDiagnostic] = []
    folds = 0
    cointegrated_folds = 0
    cursor = cfg.train_bars
    while cursor + 2 < len(bars):
        test_end = min(cursor + cfg.test_bars, len(bars))
        if test_end - cursor < 24:
            break
        folds += 1
        train = bars.iloc[cursor - cfg.train_bars : cursor]
        test = bars.iloc[cursor:test_end].reset_index(drop=True)
        model = fit_pair_model(train["close_a"].to_numpy(), train["close_b"].to_numpy())
        is_cointegrated = model.adf_t <= cfg.adf_critical
        cointegrated_folds += int(is_cointegrated)
        fold_diagnostics.append(
            FoldDiagnostic(
                fold=folds,
                train_start=pd.Timestamp(train.timestamp.iloc[0]).isoformat(),
                train_end=pd.Timestamp(train.timestamp.iloc[-1]).isoformat(),
                test_start=pd.Timestamp(test.timestamp.iloc[0]).isoformat(),
                test_end=pd.Timestamp(test.timestamp.iloc[-1]).isoformat(),
                beta=model.beta,
                adf_t=model.adf_t,
                adf_critical=cfg.adf_critical,
                cointegrated=is_cointegrated,
                regime_means=model.hmm.means,
                regime_sigmas=(
                    math.sqrt(model.hmm.variances[0]),
                    math.sqrt(model.hmm.variances[1]),
                ),
                transition=model.hmm.transition,
            )
        )
        probability = model.last_probability
        position: dict[str, Any] | None = None

        for idx in range(len(test) - 1):
            row = test.iloc[idx]
            spread = math.log(float(row.close_a)) - (
                model.alpha + model.beta * math.log(float(row.close_b))
            )
            probability = filter_probability(spread, probability, model.hmm)
            state = int(np.argmax(probability))
            state_probability = probability[state]
            sigma = math.sqrt(model.hmm.variances[state])
            z = (spread - model.hmm.means[state]) / max(sigma, math.sqrt(_EPS))
            next_row = test.iloc[idx + 1]

            if position is not None:
                held = idx - int(position["signal_index"])
                side = int(position["side"])
                reason: str | None = None
                if state != int(position["state"]) or state_probability < cfg.exit_probability:
                    reason = "regime_break"
                elif (side > 0 and z >= 0) or (side < 0 and z <= 0):
                    reason = "mean_reverted"
                elif (side > 0 and z <= -cfg.stop_z) or (side < 0 and z >= cfg.stop_z):
                    reason = "z_stop"
                elif held >= cfg.max_hold_bars:
                    reason = "time_stop"
                if reason is not None:
                    gross = _trade_return_bps(
                        side=side,
                        beta=model.beta,
                        entry_a=float(position["entry_a"]),
                        entry_b=float(position["entry_b"]),
                        exit_a=float(next_row.open_a),
                        exit_b=float(next_row.open_b),
                    )
                    trades.append(
                        PairTrade(
                            fold=folds,
                            entry_time=str(position["entry_time"]),
                            exit_time=pd.Timestamp(next_row.timestamp).isoformat(),
                            side="long_spread" if side > 0 else "short_spread",
                            entry_state=int(position["state"]),
                            entry_probability=float(position["probability"]),
                            entry_z=float(position["z"]),
                            expected_edge_bps=float(position["edge"]),
                            hold_bars=held + 1,
                            exit_reason=reason,
                            gross_bps=gross,
                            execution_cost_bps=cfg.execution_cost_bps,
                            gate_cost_bps=cfg.gate_cost_bps,
                            net_execution_bps=gross - cfg.execution_cost_bps,
                            net_gate_bps=gross - cfg.gate_cost_bps,
                        )
                    )
                    position = None
                continue

            if not is_cointegrated or state_probability < cfg.regime_probability:
                continue
            if abs(z) < cfg.entry_z:
                continue
            edge_bps = abs(spread - model.hmm.means[state]) / (1.0 + abs(model.beta)) * 10_000.0
            if edge_bps < cfg.gate_cost_bps + cfg.min_net_edge_bps:
                continue
            side = -1 if z > 0 else 1
            position = {
                "signal_index": idx,
                "entry_time": pd.Timestamp(next_row.timestamp).isoformat(),
                "entry_a": float(next_row.open_a),
                "entry_b": float(next_row.open_b),
                "side": side,
                "state": state,
                "probability": state_probability,
                "z": z,
                "edge": edge_bps,
            }

        cursor = test_end

    gross_values = [item.gross_bps for item in trades]
    net_values = [item.net_execution_bps for item in trades]
    gross = float(sum(gross_values))
    execution_cost = float(len(trades) * cfg.execution_cost_bps)
    gate_cost = float(len(trades) * cfg.gate_cost_bps)
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    equity = np.cumsum(np.asarray(net_values, dtype=float)) if net_values else np.asarray([])
    if len(equity):
        peaks = np.maximum.accumulate(np.concatenate(([0.0], equity)))[:-1]
        max_drawdown = float(np.max(peaks - equity))
    else:
        max_drawdown = 0.0
    profit_factor = None
    if losses:
        profit_factor = float(sum(wins) / abs(sum(losses)))
    elif wins:
        profit_factor = math.inf
    verdict = "INSUFFICIENT"
    if folds and cointegrated_folds == 0:
        verdict = "REJECT_NO_COINTEGRATED_FOLDS"
    elif len(trades) >= 30:
        verdict = "PROMISING_RESEARCH_ONLY" if sum(net_values) > 0 else "REJECT_AFTER_COST"

    return RegimeStatArbResult(
        strategy_id="regime_stat_arb_pair_v1",
        symbols=symbols,
        timeframe=timeframe,
        start=pd.Timestamp(bars.timestamp.iloc[0]).isoformat(),
        end=pd.Timestamp(bars.timestamp.iloc[-1]).isoformat(),
        bars=len(bars),
        folds=folds,
        cointegrated_folds=cointegrated_folds,
        fold_diagnostics=tuple(fold_diagnostics),
        trades=tuple(trades),
        gross_bps=gross,
        execution_cost_bps=execution_cost,
        gate_cost_bps=gate_cost,
        net_execution_bps=float(sum(net_values)),
        net_gate_bps=float(sum(item.net_gate_bps for item in trades)),
        win_rate=(float(len(wins) / len(trades)) if trades else None),
        profit_factor=profit_factor,
        max_drawdown_bps=max_drawdown,
        performance_eligible=False,
        funding_included=False,
        verdict=verdict,
    )


def _get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=20.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Delta response must be an object")
    return payload


async def fetch_delta_price_history(
    symbol: str,
    *,
    days: int,
    resolution: str = "1h",
    base_url: str = DELTA_INDIA_API_URL,
    end_s: int | None = None,
    page_interval_seconds: float = 0.35,
) -> pd.DataFrame:
    """Fetch closed production Delta candles for a research input frame."""
    step = _RESOLUTION_SECONDS.get(resolution)
    if step is None:
        raise ValueError(f"unsupported resolution: {resolution}")
    native = delta_native_symbol(symbol)
    stop = (int(time.time()) if end_s is None else int(end_s)) // step * step
    start = stop - int(days) * 86_400
    window = step * _MAX_BARS_PER_PAGE
    rows: list[dict[str, Any]] = []
    budget = DeltaRestBudget()
    cursor = start
    while cursor < stop:
        window_end = min(cursor + window, stop)
        query = urllib.parse.urlencode(
            {"resolution": resolution, "symbol": native, "start": cursor, "end": window_end}
        )
        wait = budget.reserve("GET", "/v2/history/candles", now=time.time())
        if wait > 0:
            await asyncio.sleep(wait)
        payload = await asyncio.to_thread(_get_json, f"{base_url}/v2/history/candles?{query}")
        if not payload.get("success", False):
            raise ValueError(f"Delta candle API error for {native}: {payload!r}")
        rows.extend(item for item in (payload.get("result") or []) if isinstance(item, dict))
        cursor = window_end
        if cursor < stop and page_interval_seconds > 0:
            await asyncio.sleep(page_interval_seconds)
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    frame = frame[columns].drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    # The bucket whose close equals ``stop`` is the last admissible closed bar.
    frame = frame[frame["timestamp"].astype("int64") // 1_000_000_000 + step <= stop]
    return frame.reset_index(drop=True)


async def _run_cli(args: argparse.Namespace) -> RegimeStatArbResult:
    end_s = int(pd.Timestamp(args.end, tz="UTC").timestamp()) if args.end else None
    asset_a, asset_b = await asyncio.gather(
        fetch_delta_price_history(args.asset_a, days=args.days, resolution=args.timeframe, end_s=end_s),
        fetch_delta_price_history(args.asset_b, days=args.days, resolution=args.timeframe, end_s=end_s),
    )
    config = RegimeStatArbConfig(
        train_bars=args.train_days * 24,
        test_bars=args.test_days * 24,
        adf_critical=args.adf_critical,
        regime_probability=args.regime_probability,
        entry_z=args.entry_z,
    )
    return run_regime_stat_arb(
        asset_a,
        asset_b,
        symbols=(delta_native_symbol(args.asset_a), delta_native_symbol(args.asset_b)),
        timeframe=args.timeframe,
        config=config,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Delta regime-aware pair research")
    parser.add_argument("--asset-a", default="BTCUSD")
    parser.add_argument("--asset-b", default="ETHUSD")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--timeframe", choices=tuple(_RESOLUTION_SECONDS), default="1h")
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--test-days", type=int, default=14)
    parser.add_argument("--adf-critical", type=float, default=-3.34)
    parser.add_argument("--regime-probability", type=float, default=0.65)
    parser.add_argument("--entry-z", type=float, default=1.645)
    parser.add_argument("--end", help="optional UTC ISO end timestamp")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(_run_cli(args))
    text = json.dumps(result.to_dict(), indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
