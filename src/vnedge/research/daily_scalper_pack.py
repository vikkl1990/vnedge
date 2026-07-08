"""Daily scalper research runner.

This mines the practical intraday scalper shape:

* 4h/1h context permission
* 15m setup candles
* 1m trigger confirmation
* isolated Quant Signal Pack families

It is research-only. A PASS here is a prompt for a pre-registered untouched
judgment window, not permission to trade.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import PromotionGates, param_grid, walk_forward
from vnedge.data.parquet_store import ParquetStore
from vnedge.research.continuous_research import wf_record
from vnedge.research.edge_leaderboard import build_edge_leaderboard
from vnedge.research.universe import load_research_targets
from vnedge.strategy.daily_scalper_pack import DAILY_SCALPER_FAMILIES, DailyScalperPack

BASE_TIMEFRAME = "15m"
CONTEXT_TIMEFRAMES = ("1h", "4h")
TRIGGER_TIMEFRAME = "1m"

DAILY_SCALPER_GATES = PromotionGates(
    min_splits=3,
    min_total_oos_trades=20,
    min_profit_factor=1.25,
    max_window_drawdown_pct=8.0,
    reject_zero_trade_windows=False,
    min_windows_with_trades_pct=60.0,
    min_payoff_ratio=1.25,
    max_single_trade_profit_share=0.35,
)


@dataclass(frozen=True)
class DailyScalperCandidate:
    exchange: str
    symbol: str
    family: str
    allowed_sides: tuple[str, ...] = ("long", "short")

    @property
    def candidate_id(self) -> str:
        safe_symbol = (
            self.symbol.replace("/", "")
            .replace(":", "")
            .replace("-", "")
            .replace("_", "")
            .upper()
        )
        return f"daily_scalper_pack_v1__{self.exchange}__{safe_symbol}__{self.family}"


def default_candidates(*, max_candidates: int | None = None) -> tuple[DailyScalperCandidate, ...]:
    """Build the full configured daily-scalper research universe.

    This is still bounded by the repo's exchange/symbol environment variables,
    but it no longer leaves most pairs/families invisible to the daily scalper
    miner.
    """
    out: list[DailyScalperCandidate] = []
    for target in load_research_targets(timeframe=BASE_TIMEFRAME):
        for family in DAILY_SCALPER_FAMILIES:
            out.append(DailyScalperCandidate(target.exchange, target.symbol, family))
    return tuple(out[:max_candidates] if max_candidates is not None else out)


def run_daily_scalper_research(
    data_root: Path | str,
    *,
    candidates: Iterable[DailyScalperCandidate] | None = None,
    max_candidates: int | None = None,
    lookback_days: int = 120,
    train_days: int = 30,
    test_days: int = 7,
    require_1m_trigger: bool = True,
) -> dict:
    store = ParquetStore(data_root)
    records: list[dict] = []
    candidate_list = (
        tuple(candidates)
        if candidates is not None else default_candidates(max_candidates=max_candidates)
    )
    if candidates is not None and max_candidates is not None:
        candidate_list = candidate_list[:max_candidates]
    for candidate in candidate_list:
        records.append(
            run_candidate(
                store,
                candidate,
                lookback_days=lookback_days,
                train_days=train_days,
                test_days=test_days,
                require_1m_trigger=require_1m_trigger,
            )
        )
    return build_daily_scalper_report(
        records,
        candidates=candidate_list,
        lookback_days=lookback_days,
        train_days=train_days,
        test_days=test_days,
        require_1m_trigger=require_1m_trigger,
    )


def run_candidate(
    store: ParquetStore,
    candidate: DailyScalperCandidate,
    *,
    lookback_days: int,
    train_days: int,
    test_days: int,
    require_1m_trigger: bool,
) -> dict:
    try:
        candles_15m = store.read_candles(
            candidate.exchange, candidate.symbol, BASE_TIMEFRAME
        )
        context_1h = store.read_candles(candidate.exchange, candidate.symbol, "1h")
        context_4h = store.read_candles(candidate.exchange, candidate.symbol, "4h")
        trigger_1m = store.read_candles(candidate.exchange, candidate.symbol, "1m")
    except FileNotFoundError as exc:
        return _untestable_record(candidate, f"missing data lane: {exc}")

    candles_15m = _window(candles_15m, lookback_days=lookback_days)
    if candles_15m.empty:
        return _untestable_record(candidate, "15m base lane has no rows in lookback window")

    latest = candles_15m["timestamp"].iloc[-1]
    context_cutoff = latest - pd.Timedelta(days=lookback_days + 40)
    trigger_cutoff = latest - pd.Timedelta(days=lookback_days + 2)
    context_1h = context_1h[context_1h["timestamp"] >= context_cutoff].reset_index(drop=True)
    context_4h = context_4h[context_4h["timestamp"] >= context_cutoff].reset_index(drop=True)
    trigger_1m = trigger_1m[trigger_1m["timestamp"] >= trigger_cutoff].reset_index(drop=True)

    train_bars = train_days * 24 * 4
    test_bars = test_days * 24 * 4
    if len(candles_15m) < train_bars + test_bars:
        return _untestable_record(
            candidate,
            f"only {len(candles_15m)} 15m bars; need >= {train_bars + test_bars}",
        )

    funding = _empty_funding_frame()
    config = BacktestConfig(max_holding_bars=16)
    grid = param_grid(
        structure_window=[24, 32],
        min_score=[4.0, 4.5],
        stop_atr_mult=[0.9, 1.1],
        take_profit_r=[1.2, 1.5],
    )

    def factory(**params):
        return DailyScalperPack(
            funding=funding,
            context_1h=context_1h,
            context_4h=context_4h,
            trigger_1m=trigger_1m,
            allowed_families=(candidate.family,),
            allowed_sides=candidate.allowed_sides,
            require_1m_trigger=require_1m_trigger,
            **params,
        )

    try:
        result = walk_forward(
            candles_15m,
            funding,
            factory,
            grid,
            config,
            train_bars=train_bars,
            test_bars=test_bars,
            symbol=candidate.symbol,
            timeframe=BASE_TIMEFRAME,
        )
    except ValueError as exc:
        return _untestable_record(candidate, str(exc))

    record = wf_record(
        "daily_scalper_pack_v1",
        candidate.symbol,
        result,
        DAILY_SCALPER_GATES,
        gates_label="daily_scalper",
        exchange=candidate.exchange,
        timeframe=BASE_TIMEFRAME,
    )
    record.update(
        {
            "candidate_id": candidate.candidate_id,
            "candidate_family": candidate.family,
            "allowed_sides": list(candidate.allowed_sides),
            "base_timeframe": BASE_TIMEFRAME,
            "context_timeframes": list(CONTEXT_TIMEFRAMES),
            "trigger_timeframe": TRIGGER_TIMEFRAME,
            "route_policy": "research_only_maker_first_until_paper_proven",
            "can_trade": False,
            "can_promote": False,
            "requires_human_approval": True,
            "requires_untouched_judgment": True,
        }
    )
    return record


def build_daily_scalper_report(
    records: list[dict],
    *,
    candidates: tuple[DailyScalperCandidate, ...],
    lookback_days: int,
    train_days: int,
    test_days: int,
    require_1m_trigger: bool,
) -> dict:
    leaderboard = build_edge_leaderboard(records)
    return {
        "updated": datetime.now(UTC).isoformat(),
        "strategy": "daily_scalper_pack_v1",
        "policy": {
            "can_trade": False,
            "can_promote": False,
            "research_only": True,
            "requires_untouched_judgment": True,
            "requires_human_approval": True,
            "route_policy": "maker-first; taker only after strong PF/payoff proof",
            "minimum_expectation": "net positive after fees in OOS walk-forward",
        },
        "flow": [
            "select_candidate_from_edge_leaderboard",
            "load_4h_1h_15m_1m_lanes",
            "train_on_15m_setups",
            "gate_by_4h_1h_context",
            "confirm_with_1m_trigger",
            "walk_forward_after_fees",
            "pre_register_untouched_judgment_if_candidate",
        ],
        "parameters": {
            "lookback_days": lookback_days,
            "train_days": train_days,
            "test_days": test_days,
            "base_timeframe": BASE_TIMEFRAME,
            "context_timeframes": list(CONTEXT_TIMEFRAMES),
            "trigger_timeframe": TRIGGER_TIMEFRAME,
            "require_1m_trigger": require_1m_trigger,
            "max_holding_bars_15m": 16,
            "gates": {
                "min_splits": DAILY_SCALPER_GATES.min_splits,
                "min_total_oos_trades": DAILY_SCALPER_GATES.min_total_oos_trades,
                "min_profit_factor": DAILY_SCALPER_GATES.min_profit_factor,
                "min_payoff_ratio": DAILY_SCALPER_GATES.min_payoff_ratio,
                "max_window_drawdown_pct": DAILY_SCALPER_GATES.max_window_drawdown_pct,
            },
        },
        "candidates": [asdict(candidate) for candidate in candidates],
        "summary": _summary(records),
        "results": records,
        "edge_leaderboard": leaderboard,
        "note": (
            "Research verdict only. PASS means candidate for untouched judgment; "
            "it does not enable paper, shadow, or live trading."
        ),
    }


def _summary(records: list[dict]) -> dict:
    testable = [r for r in records if r.get("verdict") != "UNTESTABLE"]
    passes = [r for r in records if r.get("verdict") == "PASS"]
    best = max(
        testable,
        key=lambda r: float(r.get("oos_net_usd", 0.0)),
        default=None,
    )
    return {
        "candidates": len(records),
        "testable": len(testable),
        "untestable": len(records) - len(testable),
        "passes": len(passes),
        "positive_after_fees": sum(
            1 for r in testable if float(r.get("oos_net_usd", 0.0)) > 0.0
        ),
        "best": {
            "candidate_id": best.get("candidate_id"),
            "exchange": best.get("exchange"),
            "symbol": best.get("symbol"),
            "family": best.get("candidate_family"),
            "verdict": best.get("verdict"),
            "oos_net_usd": best.get("oos_net_usd"),
            "oos_trades": best.get("oos_trades"),
            "profit_factor": best.get("profit_factor"),
        }
        if best
        else None,
    }


def _untestable_record(candidate: DailyScalperCandidate, reason: str) -> dict:
    return {
        "exchange": candidate.exchange,
        "symbol": candidate.symbol,
        "timeframe": BASE_TIMEFRAME,
        "strategy": "daily_scalper_pack_v1",
        "candidate_id": candidate.candidate_id,
        "candidate_family": candidate.family,
        "allowed_sides": list(candidate.allowed_sides),
        "base_timeframe": BASE_TIMEFRAME,
        "context_timeframes": list(CONTEXT_TIMEFRAMES),
        "trigger_timeframe": TRIGGER_TIMEFRAME,
        "route_policy": "research_only_maker_first_until_paper_proven",
        "windows": 0,
        "traded_windows": 0,
        "oos_trades": 0,
        "oos_net_usd": 0.0,
        "profitable_windows_pct": 0.0,
        "total_fees_usd": 0.0,
        "profit_factor": 0.0,
        "payoff_ratio": 0.0,
        "verdict": "UNTESTABLE",
        "reasons": [reason],
        "updated": datetime.now(UTC).isoformat(),
        "can_trade": False,
        "can_promote": False,
        "requires_human_approval": True,
        "requires_untouched_judgment": True,
    }


def _window(candles: pd.DataFrame, *, lookback_days: int) -> pd.DataFrame:
    if candles.empty:
        return candles
    cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=lookback_days)
    return candles[candles["timestamp"] >= cutoff].reset_index(drop=True)


def _empty_funding_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "funding_rate": pd.Series(dtype="float64"),
        }
    )


def parse_candidate(value: str) -> DailyScalperCandidate:
    parts = value.split("|")
    if len(parts) not in {3, 4}:
        raise argparse.ArgumentTypeError(
            "candidate must be exchange|symbol|family or exchange|symbol|family|side1,side2"
        )
    sides = (
        tuple(side.strip() for side in parts[3].split(",") if side.strip())
        if len(parts) == 4
        else ("long", "short")
    )
    return DailyScalperCandidate(parts[0], parts[1], parts[2], sides)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run VNEDGE daily scalper research")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--out", default="research/live_research/daily_scalper_latest.json")
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--train-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=7)
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--no-1m-trigger",
        action="store_true",
        help="diagnostic only: allow setup without 1m confirmation when 1m lane is sparse",
    )
    parser.add_argument(
        "--candidate",
        type=parse_candidate,
        action="append",
        default=None,
        help="exchange|symbol|family or exchange|symbol|family|long,short",
    )
    parser.add_argument("--json", action="store_true", help="print full report JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        report = _run_and_publish(args)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(_format_summary(report))
        if args.once or args.interval_seconds <= 0:
            return 0
        time.sleep(max(args.interval_seconds, 1))


def _run_and_publish(args: argparse.Namespace) -> dict:
    report = run_daily_scalper_research(
        args.data_root,
        candidates=tuple(args.candidate) if args.candidate else None,
        max_candidates=args.max_candidates,
        lookback_days=args.lookback_days,
        train_days=args.train_days,
        test_days=args.test_days,
        require_1m_trigger=not args.no_1m_trigger,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str))
    tmp.replace(out)
    return report


def _format_summary(report: dict) -> str:
    lines = [
        "=== Daily scalper pack ===",
        f"updated: {report['updated']}",
        (
            "summary: "
            f"{report['summary']['passes']} pass, "
            f"{report['summary']['positive_after_fees']} positive-after-fees, "
            f"{report['summary']['untestable']} untestable"
        ),
    ]
    for record in report["results"]:
        reasons = "; ".join(record.get("reasons", [])[:2]) or "ok"
        lines.append(
            f"  {record['exchange']} {record['symbol']} {record['candidate_family']}: "
            f"{record['verdict']} net=${float(record.get('oos_net_usd', 0.0)):+.2f} "
            f"trades={record.get('oos_trades', 0)} "
            f"pf={float(record.get('profit_factor', 0.0)):.2f} — {reasons}"
        )
    lines.append("output is research-only; no paper/shadow/live state changed")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
