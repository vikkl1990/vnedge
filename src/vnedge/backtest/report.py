"""Canonical, JSON-safe backtest report for the research dashboard.

The backtester remains the source of truth.  This module only turns one
``BacktestResult`` into a durable read model; it does not run strategies,
select parameters, promote candidates, or place orders.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from vnedge.backtest.backtester import BacktestResult, Trade
from vnedge.backtest.metrics import compute_metrics

REPORT_SCHEMA = "vnedge.backtest_report.v1"
MAX_CURVE_POINTS = 1_500


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iso(value: Any) -> str:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return stamp.isoformat()


def _downsample_indices(length: int, limit: int = MAX_CURVE_POINTS) -> list[int]:
    if length <= limit:
        return list(range(length))
    # Preserve both endpoints and distribute the remaining samples uniformly.
    return sorted({round(i * (length - 1) / (limit - 1)) for i in range(limit)})


def _longest_streak(values: Iterable[float], *, winning: bool) -> int:
    longest = current = 0
    for value in values:
        match = value > 0 if winning else value <= 0
        current = current + 1 if match else 0
        longest = max(longest, current)
    return longest


def _longest_underwater_days(curve: pd.Series) -> float:
    if curve.empty:
        return 0.0
    peak = -math.inf
    underwater_since: pd.Timestamp | None = None
    longest = pd.Timedelta(0)
    for raw_ts, raw_equity in curve.items():
        ts = pd.Timestamp(raw_ts)
        equity = float(raw_equity)
        if equity >= peak:
            if underwater_since is not None:
                longest = max(longest, ts - underwater_since)
                underwater_since = None
            peak = equity
        elif underwater_since is None:
            underwater_since = ts
    if underwater_since is not None:
        longest = max(longest, pd.Timestamp(curve.index[-1]) - underwater_since)
    return round(longest.total_seconds() / 86_400.0, 3)


def _trade_row(trade: Trade, initial_equity: float) -> dict[str, Any]:
    net = trade.net_pnl_usd
    notional = abs(trade.quantity * trade.entry_price)
    hold_seconds = max(
        0.0,
        (pd.Timestamp(trade.exit_ts) - pd.Timestamp(trade.entry_ts)).total_seconds(),
    )
    return {
        "side": trade.side,
        "quantity": trade.quantity,
        "entry_ts": _iso(trade.entry_ts),
        "entry_price": trade.entry_price,
        "exit_ts": _iso(trade.exit_ts),
        "exit_price": trade.exit_price,
        "exit_reason": trade.exit_reason,
        "entry_reason": trade.entry_reason,
        "gross_pnl_usd": trade.gross_pnl_usd,
        "fees_usd": trade.fees_usd,
        "funding_usd": trade.funding_usd,
        "net_pnl_usd": net,
        "net_bps_on_entry_notional": (net / notional * 10_000.0) if notional else None,
        "return_on_initial_equity_pct": (net / initial_equity * 100.0),
        "mae_usd": trade.mae_usd,
        "mfe_usd": trade.mfe_usd,
        "hold_seconds": hold_seconds,
    }


def _daily_rows(trades: tuple[Trade, ...], curve: pd.Series) -> list[dict[str, Any]]:
    realized: dict[str, list[float]] = {}
    for trade in trades:
        day = pd.Timestamp(trade.exit_ts).strftime("%Y-%m-%d")
        realized.setdefault(day, []).append(trade.net_pnl_usd)

    daily_equity = curve.resample("1D").last().dropna() if not curve.empty else curve
    rows: list[dict[str, Any]] = []
    previous: float | None = None
    running_peak = -math.inf
    for raw_ts, raw_equity in daily_equity.items():
        equity = float(raw_equity)
        running_peak = max(running_peak, equity)
        day = pd.Timestamp(raw_ts).strftime("%Y-%m-%d")
        pnls = realized.get(day, [])
        rows.append(
            {
                "date": day,
                "net_pnl_usd": sum(pnls),
                "trade_count": len(pnls),
                "wins": sum(1 for value in pnls if value > 0),
                "losses": sum(1 for value in pnls if value <= 0),
                "equity_usd": equity,
                "equity_change_usd": 0.0 if previous is None else equity - previous,
                "drawdown_pct": (
                    (equity / running_peak - 1.0) * 100.0 if running_peak > 0 else 0.0
                ),
            }
        )
        previous = equity
    return rows


def _monthly_rows(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_month: dict[str, list[dict[str, Any]]] = {}
    for row in daily:
        by_month.setdefault(row["date"][:7], []).append(row)
    rows: list[dict[str, Any]] = []
    for month, values in sorted(by_month.items()):
        pnls = [float(row["net_pnl_usd"]) for row in values]
        drawdowns = [float(row["drawdown_pct"]) for row in values]
        rows.append(
            {
                "month": month,
                "net_pnl_usd": sum(pnls),
                "traded_days": sum(1 for row in values if row["trade_count"]),
                "trade_count": sum(int(row["trade_count"]) for row in values),
                "win_days": sum(1 for value in pnls if value > 0),
                "loss_days": sum(1 for value in pnls if value < 0),
                "best_day_usd": max(pnls, default=0.0),
                "worst_day_usd": min(pnls, default=0.0),
                "max_drawdown_pct": min(drawdowns, default=0.0),
                "close_equity_usd": values[-1]["equity_usd"],
            }
        )
    return rows


def build_backtest_report(
    result: BacktestResult,
    *,
    run_id: str,
    strategy_id: str,
    exchange: str,
    data_source: str,
    bars: int,
    parameters: dict[str, Any] | None = None,
    generated_at: str | None = None,
    evidence_class: str = "EXPLORATORY",
    engine: str = "vnedge.backtest.run_backtest",
) -> dict[str, Any]:
    """Build the complete report consumed by ``/backtest-lab``."""
    metrics = compute_metrics(result)
    curve = result.equity_curve.sort_index()
    initial = float(result.config.initial_equity_usd)
    trades = tuple(result.trades)
    trade_rows = [_trade_row(trade, initial) for trade in trades]
    daily = _daily_rows(trades, curve)
    monthly = _monthly_rows(daily)
    pnls = [trade.net_pnl_usd for trade in trades]
    gross_profit = sum(trade.gross_pnl_usd for trade in trades)
    fees = sum(trade.fees_usd for trade in trades)
    funding = sum(trade.funding_usd for trade in trades)
    net_profit = gross_profit - fees + funding
    wins = [value for value in pnls if value > 0]
    duration_days = 0.0
    if len(curve) > 1:
        duration_days = max(
            0.0,
            (pd.Timestamp(curve.index[-1]) - pd.Timestamp(curve.index[0])).total_seconds()
            / 86_400.0,
        )
    annualized_return = None
    if duration_days > 0 and initial > 0 and result.final_equity_usd > 0:
        annualized_return = (
            (result.final_equity_usd / initial) ** (365.0 / duration_days) - 1.0
        ) * 100.0
    calmar = (
        annualized_return / metrics.max_drawdown_pct
        if annualized_return is not None and metrics.max_drawdown_pct > 0
        else None
    )
    traded = [row for row in daily if row["trade_count"] > 0]
    day_pnls = [float(row["net_pnl_usd"]) for row in traded]
    curve_points = []
    peak = -math.inf
    for index in _downsample_indices(len(curve)):
        ts = curve.index[index]
        equity = float(curve.iloc[index])
        peak = max(peak, equity)
        curve_points.append(
            {
                "ts": _iso(ts),
                "equity_usd": equity,
                "drawdown_pct": (equity / peak - 1.0) * 100.0 if peak > 0 else 0.0,
            }
        )

    warnings: list[str] = []
    if len(trades) < 30:
        warnings.append("UNDER_SAMPLED: fewer than 30 closed trades")
    if evidence_class.upper() != "SEALED_OOS":
        warnings.append("Not sealed OOS evidence; this run cannot support promotion")
    if not trades:
        warnings.append("No closed trades in the selected window")

    best_trade = max(pnls, default=0.0)
    worst_trade = min(pnls, default=0.0)
    gross_wins = sum(wins)
    return {
        "schema": REPORT_SCHEMA,
        "run": {
            "run_id": run_id,
            "status": "COMPLETE",
            "generated_at": generated_at or datetime.now(UTC).isoformat(),
            "engine": engine,
            "evidence_class": evidence_class,
            "strategy_id": strategy_id,
            "exchange": exchange,
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "data_source": data_source,
            "bars": bars,
            "window": {
                "start": _iso(curve.index[0]) if not curve.empty else None,
                "end": _iso(curve.index[-1]) if not curve.empty else None,
                "duration_days": duration_days,
            },
            "parameters": parameters or {},
            "initial_equity_usd": initial,
            "costs": {
                "maker_bps_per_leg": result.config.fees.maker_bps,
                "taker_bps_per_leg": result.config.fees.taker_bps,
                "slippage_bps_per_leg": result.config.slippage.bps,
                "modeled_taker_round_trip_bps": (
                    2.0
                    * (result.config.fees.taker_bps + result.config.slippage.bps)
                ),
                "funding_included": True,
            },
            "exit_contract": {
                "max_holding_bars": result.config.max_holding_bars,
                "active_exit": result.config.use_active_exit,
                "partial_take_profit": result.config.allow_partial_tp,
                "trail_atr_mult": result.config.trail_atr_mult,
                "fee_aware_breakeven_bps": result.config.fee_aware_breakeven_bps,
            },
        },
        "overview": {
            **metrics.to_dict(),
            "gross_profit_usd": gross_profit,
            "net_profit_usd": net_profit,
            "total_cost_usd": fees - funding,
            "annualized_return_pct": annualized_return,
            "calmar": calmar,
            "traded_days": len(traded),
            "win_days": sum(1 for value in day_pnls if value > 0),
            "loss_days": sum(1 for value in day_pnls if value < 0),
            "avg_day_pnl_usd": sum(day_pnls) / len(day_pnls) if day_pnls else 0.0,
            "median_day_pnl_usd": float(pd.Series(day_pnls).median()) if day_pnls else 0.0,
            "best_day_usd": max(day_pnls, default=0.0),
            "worst_day_usd": min(day_pnls, default=0.0),
            "best_trade_usd": best_trade,
            "worst_trade_usd": worst_trade,
            "max_win_streak": _longest_streak(pnls, winning=True),
            "max_loss_streak": _longest_streak(pnls, winning=False),
            "longest_underwater_days": _longest_underwater_days(curve),
            "avg_hold_hours": (
                sum(row["hold_seconds"] for row in trade_rows) / len(trade_rows) / 3_600.0
                if trade_rows
                else 0.0
            ),
            "best_trade_profit_share_pct": (
                max(wins) / gross_wins * 100.0 if wins and gross_wins > 0 else None
            ),
        },
        "equity_curve": curve_points,
        "daily": daily,
        "monthly": monthly,
        "trades": trade_rows,
        "warnings": warnings,
        "governance": {
            "can_trade": False,
            "can_promote": False,
            "read_only": True,
            "promotion_requires_separate_untouched_judgment": True,
        },
    }
