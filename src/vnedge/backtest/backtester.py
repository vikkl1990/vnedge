"""Event-loop candle backtester for USDT-margined perpetuals.

Realism rules (docs/DESIGN.md, backtesting rules):

- Decisions at bar close, fills at next bar open. Lookahead is structurally
  impossible: the engine, not the strategy, controls what data exists at
  decision time.
- Taker fees and adverse slippage on every fill.
- Funding cashflows applied at the actual timestamps from the ingested
  funding series (longs pay positive rates, shorts receive).
- Intrabar exits are conservative: if both stop and take-profit lie inside
  one bar's range, the STOP is assumed to fill. Stops fill with slippage.
- Position size comes from the SAME ``size_position`` function live trading
  uses — backtest and live can never disagree on sizing math. If sizing
  rejects (exchange minimums, exposure caps), the trade is skipped and
  counted, never force-fitted.
- One position at a time, one symbol per run (v1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd
from pydantic import BaseModel, Field

from vnedge.backtest.fee_model import FeeModel
from vnedge.backtest.slippage_model import SlippageModel
from vnedge.config.risk_config import RiskConfig
from vnedge.risk.position_sizer import SymbolLimits, size_position
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

logger = logging.getLogger(__name__)


class BacktestConfig(BaseModel):
    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    initial_equity_usd: float = Field(default=500.0, gt=0)
    max_holding_bars: int = Field(default=48, ge=1)
    fees: FeeModel = Field(default_factory=FeeModel)
    slippage: SlippageModel = Field(default_factory=SlippageModel)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    limits: SymbolLimits = Field(
        default=SymbolLimits(
            min_qty=0.0001, qty_step=0.0001, min_notional_usd=5.0,
            maintenance_margin_rate=0.005,
        )
    )


@dataclass(frozen=True)
class Trade:
    side: str
    quantity: float
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp
    exit_price: float
    exit_reason: str  # "stop" | "take_profit" | "max_holding" | "end_of_data"
    gross_pnl_usd: float
    fees_usd: float
    funding_usd: float  # negative = paid
    entry_reason: str
    mae_usd: float = 0.0  # max adverse excursion while open (<= 0)
    mfe_usd: float = 0.0  # max favorable excursion while open (>= 0)

    @property
    def net_pnl_usd(self) -> float:
        return self.gross_pnl_usd - self.fees_usd + self.funding_usd


@dataclass
class _OpenPosition:
    intent: SignalIntent
    quantity: float
    entry_price: float
    entry_ts: pd.Timestamp
    entry_bar: int
    entry_fee_usd: float
    funding_usd: float = 0.0
    best_price: float = 0.0
    worst_price: float = 0.0

    def __post_init__(self) -> None:
        self.best_price = self.worst_price = self.entry_price

    def track_excursion(self, high: float, low: float) -> None:
        if self.intent.side == "long":
            self.best_price = max(self.best_price, high)
            self.worst_price = min(self.worst_price, low)
        else:
            self.best_price = min(self.best_price, low)
            self.worst_price = max(self.worst_price, high)


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    timeframe: str
    trades: tuple[Trade, ...]
    equity_curve: pd.Series  # timestamp-indexed, marked at bar close
    skipped_by_sizing: int
    final_equity_usd: float
    config: BacktestConfig = field(repr=False, default_factory=BacktestConfig)


def _unrealized(pos: _OpenPosition, price: float) -> float:
    direction = 1.0 if pos.intent.side == "long" else -1.0
    return direction * pos.quantity * (price - pos.entry_price)


def _check_intrabar_exit(
    pos: _OpenPosition, high: float, low: float
) -> tuple[str, float] | None:
    """Conservative exit resolution inside one bar. Stop always wins ties."""
    intent = pos.intent
    if intent.side == "long":
        if low <= intent.stop_price:
            return "stop", intent.stop_price
        if intent.take_profit_price is not None and high >= intent.take_profit_price:
            return "take_profit", intent.take_profit_price
    else:
        if high >= intent.stop_price:
            return "stop", intent.stop_price
        if intent.take_profit_price is not None and low <= intent.take_profit_price:
            return "take_profit", intent.take_profit_price
    return None


def run_backtest(
    candles: pd.DataFrame,
    funding: pd.DataFrame | None,
    strategy: BaseStrategy,
    config: BacktestConfig,
    *,
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "1h",
) -> BacktestResult:
    if candles.empty:
        raise ValueError("empty candle frame")
    if not candles["timestamp"].is_monotonic_increasing or candles["timestamp"].duplicated().any():
        raise ValueError("candles must be gate-validated: sorted, unique timestamps")

    df = strategy.prepare(candles).reset_index(drop=True)
    if len(df) != len(candles):
        raise ValueError("strategy.prepare() must not add or drop rows")

    # Funding events as parallel arrays with a moving cursor.
    if funding is not None and not funding.empty:
        f_ts = funding["timestamp"].to_numpy()
        f_rate = funding["funding_rate"].to_numpy()
    else:
        f_ts, f_rate = [], []
    f_idx = 0

    equity = config.initial_equity_usd
    position: _OpenPosition | None = None
    pending: SignalIntent | None = None
    trades: list[Trade] = []
    skipped = 0
    curve: list[float] = []

    def close_position(pos: _OpenPosition, ts, raw_price: float, reason: str) -> None:
        nonlocal equity, position
        exit_side = "sell" if pos.intent.side == "long" else "buy"
        fill = config.slippage.fill_price(raw_price, exit_side)
        exit_fee = config.fees.taker_fee_usd(pos.quantity * fill)
        gross = _unrealized(pos, fill)
        fees = pos.entry_fee_usd + exit_fee
        equity += gross - exit_fee + pos.funding_usd
        direction = 1.0 if pos.intent.side == "long" else -1.0
        trades.append(
            Trade(
                side=pos.intent.side, quantity=pos.quantity,
                entry_ts=pos.entry_ts, entry_price=pos.entry_price,
                exit_ts=ts, exit_price=fill, exit_reason=reason,
                gross_pnl_usd=gross, fees_usd=fees, funding_usd=pos.funding_usd,
                entry_reason=pos.intent.reason,
                mae_usd=direction * pos.quantity * (pos.worst_price - pos.entry_price),
                mfe_usd=direction * pos.quantity * (pos.best_price - pos.entry_price),
            )
        )
        position = None

    n = len(df)
    start = max(strategy.warmup_bars, 1)
    timestamps = df["timestamp"]

    for j in range(start, n):
        bar = df.iloc[j]
        ts = timestamps.iloc[j]

        # 1) Funding on positions held into this bar (events in (prev_ts, ts]).
        if position is not None:
            while f_idx < len(f_ts) and f_ts[f_idx] <= ts:
                if f_ts[f_idx] > timestamps.iloc[j - 1]:
                    direction = -1.0 if position.intent.side == "long" else 1.0
                    notional = position.quantity * float(bar["close"])
                    position.funding_usd += direction * float(f_rate[f_idx]) * notional
                f_idx += 1
        else:
            while f_idx < len(f_ts) and f_ts[f_idx] <= ts:
                f_idx += 1

        # 2) Fill last bar's intent at this bar's open.
        if position is None and pending is not None:
            entry_side = "buy" if pending.side == "long" else "sell"
            fill = config.slippage.fill_price(float(bar["open"]), entry_side)
            sizing = size_position(
                equity_usd=equity, entry_price=fill, stop_price=pending.stop_price,
                side=pending.side, config=config.risk, limits=config.limits,
            )
            if sizing.approved:
                fee = config.fees.taker_fee_usd(sizing.notional_usd)
                equity -= fee
                position = _OpenPosition(
                    intent=pending, quantity=sizing.quantity, entry_price=fill,
                    entry_ts=ts, entry_bar=j, entry_fee_usd=fee,
                )
            else:
                skipped += 1
                logger.debug("sizing rejected at %s: %s", ts, sizing.reasons)
        pending = None

        # 3) Exit checks (applies to just-entered positions too — a stop can
        #    be hit in the entry bar).
        if position is not None:
            position.track_excursion(float(bar["high"]), float(bar["low"]))
            hit = _check_intrabar_exit(position, float(bar["high"]), float(bar["low"]))
            if hit is not None:
                close_position(position, ts, hit[1], hit[0])
            elif j - position.entry_bar >= config.max_holding_bars:
                close_position(position, ts, float(bar["close"]), "max_holding")

        # 4) New entry decision at this bar's close (only when flat).
        if position is None and j < n - 1 and equity > 0:
            pending = strategy.signal(df, j)

        # 5) Mark equity at bar close.
        mark = equity + (_unrealized(position, float(bar["close"])) if position else 0.0)
        curve.append(mark)

        if equity <= 0:
            logger.warning("equity depleted at %s — halting backtest", ts)
            break

    # Force-close anything still open at the last processed bar.
    if position is not None:
        last = df.iloc[start + len(curve) - 1]
        close_position(position, last["timestamp"], float(last["close"]), "end_of_data")
        curve[-1] = equity

    equity_curve = pd.Series(
        curve, index=timestamps.iloc[start : start + len(curve)], name="equity"
    )
    return BacktestResult(
        symbol=symbol, timeframe=timeframe, trades=tuple(trades),
        equity_curve=equity_curve, skipped_by_sizing=skipped,
        final_equity_usd=equity, config=config,
    )
