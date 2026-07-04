"""Research universe selection for the slow loop.

The execution roadmap is still deliberately single-exchange for V1, but the
research lab can compare venues and symbols offline. This module keeps that
universe explicit and bounded: operators choose exchanges/symbols through
environment variables, and every target is a plain exchange/symbol/timeframe
tuple used only by the research process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

DEFAULT_EXCHANGES = ("binanceusdm", "bybit", "delta")
DEFAULT_SYMBOLS = (
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "DOGE/USDT:USDT",
)
DEFAULT_TIMEFRAME = "1h"


@dataclass(frozen=True, order=True)
class ResearchTarget:
    exchange: str
    symbol: str
    timeframe: str = DEFAULT_TIMEFRAME

    @property
    def key(self) -> str:
        return f"{self.exchange}|{self.symbol}|{self.timeframe}"

    @property
    def label(self) -> str:
        return f"{self.exchange}:{self.symbol}:{self.timeframe}"


@dataclass(frozen=True)
class ProfitablePair:
    exchange: str
    symbol: str
    timeframe: str
    best_strategy: str
    verdict: str
    oos_net_usd: float
    oos_trades: int
    gates: str

    def to_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "best_strategy": self.best_strategy,
            "verdict": self.verdict,
            "oos_net_usd": self.oos_net_usd,
            "oos_trades": self.oos_trades,
            "gates": self.gates,
        }


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_suffix(exchange: str) -> str:
    return exchange.upper().replace("-", "_").replace("/", "_")


def load_research_targets(
    *,
    exchanges: Iterable[str] | None = None,
    symbols: Iterable[str] | None = None,
    timeframe: str | None = None,
) -> tuple[ResearchTarget, ...]:
    """Build the target universe.

    Env controls:
    - RESEARCH_EXCHANGES=binanceusdm,bybit,delta
    - RESEARCH_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT
    - RESEARCH_SYMBOLS_BYBIT=BTC/USDT:USDT,SOL/USDT:USDT
    - RESEARCH_TIMEFRAME=1h
    """
    selected_exchanges = tuple(exchanges or _split_csv(os.environ.get("RESEARCH_EXCHANGES"))
                              or DEFAULT_EXCHANGES)
    selected_symbols = tuple(symbols or _split_csv(os.environ.get("RESEARCH_SYMBOLS"))
                             or DEFAULT_SYMBOLS)
    selected_timeframe = timeframe or os.environ.get("RESEARCH_TIMEFRAME", DEFAULT_TIMEFRAME)

    targets: list[ResearchTarget] = []
    seen: set[str] = set()
    for exchange in selected_exchanges:
        per_exchange_symbols = (
            _split_csv(os.environ.get(f"RESEARCH_SYMBOLS_{_env_suffix(exchange)}"))
            or selected_symbols
        )
        for symbol in per_exchange_symbols:
            target = ResearchTarget(exchange=exchange, symbol=symbol,
                                    timeframe=selected_timeframe)
            if target.key not in seen:
                targets.append(target)
                seen.add(target.key)
    return tuple(targets)


def summarize_universe(targets: Iterable[ResearchTarget]) -> dict:
    targets = tuple(targets)
    by_exchange: dict[str, int] = {}
    for target in targets:
        by_exchange[target.exchange] = by_exchange.get(target.exchange, 0) + 1
    return {
        "targets": len(targets),
        "exchanges": sorted(by_exchange),
        "targets_by_exchange": by_exchange,
        "symbols": sorted({t.symbol for t in targets}),
        "timeframes": sorted({t.timeframe for t in targets}),
    }


def profitable_pairs(
    records: Iterable[dict],
    *,
    min_oos_net_usd: float = 0.0,
    min_oos_trades: int = 10,
    include_positive_rejects: bool = True,
) -> tuple[ProfitablePair, ...]:
    """Select the best currently profitable lane per exchange/symbol.

    PASS rows are strongest. Positive REJECT rows are still useful research
    targets because they can be close to passing, but they remain candidates.
    """
    best: dict[tuple[str, str, str], dict] = {}
    for record in records:
        if record.get("auto"):
            continue
        verdict = record.get("verdict", "REJECT")
        net = float(record.get("oos_net_usd", 0.0))
        trades = int(record.get("oos_trades", 0))
        profitable = net > min_oos_net_usd and trades >= min_oos_trades
        if verdict != "PASS" and not (include_positive_rejects and profitable):
            continue
        key = (
            record.get("exchange", "binanceusdm"),
            record.get("symbol", ""),
            record.get("timeframe", DEFAULT_TIMEFRAME),
        )
        score = (2 if verdict == "PASS" else 1, net, trades)
        prev = best.get(key)
        prev_score = (
            2 if prev and prev.get("verdict") == "PASS" else 1,
            float(prev.get("oos_net_usd", 0.0)) if prev else float("-inf"),
            int(prev.get("oos_trades", 0)) if prev else -1,
        )
        if prev is None or score > prev_score:
            best[key] = record

    pairs = [
        ProfitablePair(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            best_strategy=record["strategy"],
            verdict=record.get("verdict", "REJECT"),
            oos_net_usd=round(float(record.get("oos_net_usd", 0.0)), 2),
            oos_trades=int(record.get("oos_trades", 0)),
            gates=record.get("gates", "standard"),
        )
        for (exchange, symbol, timeframe), record in best.items()
    ]
    return tuple(sorted(pairs, key=lambda p: (p.verdict != "PASS", -p.oos_net_usd,
                                             p.exchange, p.symbol)))
