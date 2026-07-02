"""Walk-forward validation.

Rolling train/test windows: parameters are selected on each train window and
evaluated on the immediately following unseen test window. Only the
out-of-sample (test) results count toward strategy approval — in-sample
numbers are reported for overfitting diagnosis only.

Honesty rules built in:

- Test windows are prefixed with exactly ``warmup_bars`` of history so
  indicators are warm, but the engine cannot open trades before the test
  region begins (the warmup prefix IS the engine's no-trade warmup).
- Each window runs with fresh initial equity — no compounding across
  windows, so one lucky early window cannot inflate later ones.
- Parameter selection requires a minimum trade count; a "great" Sharpe from
  two trades is noise and scores -inf.
"""

from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from vnedge.backtest.backtester import BacktestConfig, run_backtest
from vnedge.backtest.metrics import BacktestMetrics, compute_metrics
from vnedge.strategy.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)

MIN_TRADES_FOR_SELECTION = 5

StrategyFactory = Callable[..., BaseStrategy]
SelectionScore = Callable[[BacktestMetrics], float]


def default_selection_score(m: BacktestMetrics) -> float:
    """Sharpe, but only with a statistically meaningful trade count."""
    if m.num_trades < MIN_TRADES_FOR_SELECTION:
        return -math.inf
    return m.sharpe


def param_grid(**axes: list) -> list[dict]:
    """param_grid(fast=[12, 24], slow=[72]) -> [{'fast':12,'slow':72}, ...]"""
    keys = list(axes)
    return [dict(zip(keys, combo)) for combo in itertools.product(*axes.values())]


@dataclass(frozen=True)
class WindowResult:
    window_index: int
    train_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    chosen_params: dict
    train_metrics: BacktestMetrics
    test_metrics: BacktestMetrics


@dataclass(frozen=True)
class WalkForwardResult:
    windows: tuple[WindowResult, ...]

    @property
    def oos_net_profit_usd(self) -> float:
        return sum(w.test_metrics.net_profit_usd for w in self.windows)

    @property
    def oos_profitable_window_pct(self) -> float:
        """Consistency: fraction of test windows that made money. A strategy
        that wins in 1 of 6 windows is a coin toss with survivor bias."""
        if not self.windows:
            return 0.0
        wins = sum(1 for w in self.windows if w.test_metrics.net_profit_usd > 0)
        return wins / len(self.windows) * 100.0

    @property
    def summary(self) -> str:
        lines = [
            f"walk-forward: {len(self.windows)} windows | "
            f"OOS net ${self.oos_net_profit_usd:+.2f} | "
            f"profitable windows {self.oos_profitable_window_pct:.0f}%"
        ]
        for w in self.windows:
            lines.append(
                f"  w{w.window_index} params={w.chosen_params} "
                f"IS[{w.train_metrics.summary}] OOS[{w.test_metrics.summary}]"
            )
        return "\n".join(lines)


def _slice_funding(funding: pd.DataFrame | None, start, end) -> pd.DataFrame | None:
    if funding is None or funding.empty:
        return funding
    mask = (funding["timestamp"] >= start) & (funding["timestamp"] <= end)
    return funding.loc[mask].reset_index(drop=True)


def walk_forward(
    candles: pd.DataFrame,
    funding: pd.DataFrame | None,
    strategy_factory: StrategyFactory,
    grid: list[dict],
    config: BacktestConfig,
    *,
    train_bars: int,
    test_bars: int,
    step_bars: int | None = None,
    selection: SelectionScore = default_selection_score,
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "1h",
) -> WalkForwardResult:
    if not grid:
        raise ValueError("empty parameter grid")
    if train_bars <= 0 or test_bars <= 0:
        raise ValueError("train_bars and test_bars must be positive")
    step_bars = step_bars if step_bars is not None else test_bars
    if step_bars <= 0:
        raise ValueError("step_bars must be positive")
    n = len(candles)
    if train_bars + test_bars > n:
        raise ValueError(
            f"not enough data: need {train_bars + test_bars} bars, have {n}"
        )

    windows: list[WindowResult] = []
    start = 0
    while start + train_bars + test_bars <= n:
        train = candles.iloc[start : start + train_bars].reset_index(drop=True)
        train_funding = _slice_funding(
            funding, train["timestamp"].iloc[0], train["timestamp"].iloc[-1]
        )

        # --- Select parameters on the train window only -----------------------
        best_params: dict | None = None
        best_score = -math.inf
        best_train_metrics: BacktestMetrics | None = None
        for params in grid:
            strategy = strategy_factory(**params)
            result = run_backtest(
                train, train_funding, strategy, config, symbol=symbol, timeframe=timeframe
            )
            metrics = compute_metrics(result)
            score = selection(metrics)
            if score > best_score:
                best_score, best_params, best_train_metrics = score, params, metrics

        if best_params is None or best_score == -math.inf:
            logger.warning(
                "window %d: no parameter set produced %d+ trades in-sample — skipping",
                len(windows), MIN_TRADES_FOR_SELECTION,
            )
            start += step_bars
            continue

        # --- Evaluate out-of-sample with warmup prefix ------------------------
        strategy = strategy_factory(**best_params)
        prefix = strategy.warmup_bars
        test_slice = candles.iloc[
            start + train_bars - prefix : start + train_bars + test_bars
        ].reset_index(drop=True)
        test_funding = _slice_funding(
            funding, test_slice["timestamp"].iloc[0], test_slice["timestamp"].iloc[-1]
        )
        test_result = run_backtest(
            test_slice, test_funding, strategy, config, symbol=symbol, timeframe=timeframe
        )
        test_metrics = compute_metrics(test_result)

        windows.append(
            WindowResult(
                window_index=len(windows),
                train_start=train["timestamp"].iloc[0],
                test_start=candles["timestamp"].iloc[start + train_bars],
                test_end=candles["timestamp"].iloc[start + train_bars + test_bars - 1],
                chosen_params=best_params,
                train_metrics=best_train_metrics,
                test_metrics=test_metrics,
            )
        )
        start += step_bars

    return WalkForwardResult(windows=tuple(windows))


# --------------------------------------------------------------------------
# Promotion gates — the machine-readable verdict on a walk-forward result.
# A strategy may proceed to paper trading ONLY if evaluate_promotion passes;
# the human approval gate sits after this, never instead of it.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionGates:
    min_splits: int = 3
    min_total_oos_trades: int = 10
    min_profit_factor: float = 1.1
    max_window_drawdown_pct: float = 15.0
    # If in-sample was profitable, OOS must retain at least this fraction of
    # the per-window IS net profit — otherwise the edge is fitted, not real.
    min_is_retention: float = 0.25


@dataclass(frozen=True)
class PromotionDecision:
    passed: bool
    reject_reasons: tuple[str, ...]

    @property
    def summary(self) -> str:
        if self.passed:
            return "PROMOTION GATES PASSED — eligible for paper trading (human approval still required)"
        return "PROMOTION REJECTED: " + "; ".join(self.reject_reasons)


def evaluate_promotion(
    result: WalkForwardResult, gates: PromotionGates = PromotionGates()
) -> PromotionDecision:
    reasons: list[str] = []
    windows = result.windows

    if len(windows) < gates.min_splits:
        reasons.append(
            f"only {len(windows)} valid OOS splits (need >= {gates.min_splits})"
        )

    zero_trade = [w.window_index for w in windows if w.test_metrics.num_trades == 0]
    if zero_trade:
        reasons.append(f"zero OOS trades in window(s) {zero_trade}")

    total_trades = sum(w.test_metrics.num_trades for w in windows)
    if total_trades < gates.min_total_oos_trades:
        reasons.append(
            f"{total_trades} total OOS trades (need >= {gates.min_total_oos_trades})"
        )

    oos_net = result.oos_net_profit_usd
    if oos_net <= 0:
        reasons.append(f"aggregate OOS net ${oos_net:+.2f} is not positive")

    # Aggregate profit factor from per-window gross wins/losses (recovered
    # exactly from avg_win * win_count / avg_loss * loss_count).
    gross_wins = gross_losses = 0.0
    for w in windows:
        m = w.test_metrics
        n_wins = round(m.win_rate_pct / 100.0 * m.num_trades)
        n_losses = m.num_trades - n_wins
        gross_wins += m.avg_win_usd * n_wins
        gross_losses += -m.avg_loss_usd * n_losses
    if gross_losses > 0:
        agg_pf = gross_wins / gross_losses
        if agg_pf < gates.min_profit_factor:
            reasons.append(
                f"aggregate OOS profit factor {agg_pf:.2f} < {gates.min_profit_factor}"
            )
    elif gross_wins <= 0 and windows:
        reasons.append("no OOS gross wins — profit factor 0")

    worst_dd = max((w.test_metrics.max_drawdown_pct for w in windows), default=0.0)
    if worst_dd > gates.max_window_drawdown_pct:
        reasons.append(
            f"worst OOS window drawdown {worst_dd:.1f}% > {gates.max_window_drawdown_pct}%"
        )

    # Retention is measured on raw totals without scaling IS to the shorter
    # OOS window length — stricter than pro-rata, and simpler to reason about.
    is_net = sum(w.train_metrics.net_profit_usd for w in windows)
    if is_net > 0 and oos_net < gates.min_is_retention * is_net:
        reasons.append(
            f"IS/OOS collapse: OOS net ${oos_net:+.2f} retains "
            f"{oos_net / is_net * 100.0:.0f}% of IS net ${is_net:+.2f} "
            f"(need >= {gates.min_is_retention:.0%})"
        )

    return PromotionDecision(passed=not reasons, reject_reasons=tuple(reasons))
