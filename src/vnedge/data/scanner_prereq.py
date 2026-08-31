"""Fail-closed readiness proof for scanner candle prerequisites.

The scanner runtime may seed price-only indicator history from exchange OHLCV,
but exact VWAP and volume gates must come from the canonical trade-derived
lake.  This command verifies the recent closed 5m window and every parent in
the 15m -> 1h -> 4h ladder before Docker starts observer lanes.

It is deliberately a verifier, not another backfill implementation.  The
archive bootstrap and strict REST gap recovery own mutation; this module makes
their completion an explicit startup contract.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from vnedge.data.candles import TF_SECONDS, Candle, CandleParquetStore, floor_time
from vnedge.data.structure_mtf import MTF_PARAMS
from vnedge.data.symbols import canonical_symbol
from vnedge.strategy.regime_router import DEFAULT_CONFIG as REGIME_CONFIG
from vnedge.strategy.regime_router import EXPAND_NATIVE_IDS, RANGE_NATIVE_IDS
from vnedge.strategy.strategy_registry import get_strategy_class

HTF_STRUCTURE_MIN_BARS = 2 * (MTF_PARAMS.htf_left + MTF_PARAMS.htf_right) + 4


DEFAULT_REQUIREMENTS: Mapping[str, int] = {
    # Active scanner causal contracts, including one evaluable bar after
    # indicator warmup. Readiness must prove the full exact-volume window,
    # not merely one recent day of healthy candles.
    "5m": 2066,
    "15m": 2018,
    "1h": 24,
    "4h": HTF_STRUCTURE_MIN_BARS,
}


def requirements_from_roster(path: Path | str) -> dict[str, int]:
    """Derive exact-candle depth from the configured scanner contracts.

    The roster, not an unrelated retired lane, owns startup depth. Strategies
    routed through the causal regime layer also inherit that layer's warmup.
    Small 1h/4h tails remain required for Pulse/MTF integrity.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    observers = payload.get("observers")
    if not isinstance(observers, list) or not observers:
        raise ValueError("scanner roster must contain a non-empty observers list")
    requirements: dict[str, int] = {"1h": 24, "4h": HTF_STRUCTURE_MIN_BARS}
    routed = RANGE_NATIVE_IDS | EXPAND_NATIVE_IDS
    for observer in observers:
        if not isinstance(observer, dict):
            raise TypeError("scanner roster observer must be an object")
        if observer.get("enabled", True) is False:
            continue
        strategy_id = str(observer.get("strategy_id") or "")
        timeframe = str(observer.get("timeframe") or "")
        strategy = get_strategy_class(strategy_id)
        declared = str(getattr(strategy, "timeframe", timeframe) or timeframe)
        if timeframe != declared:
            raise ValueError(
                f"{strategy_id} roster timeframe {timeframe} != declared {declared}"
            )
        needed = int(getattr(strategy, "warmup_bars", 0)) + 1
        if strategy_id in routed:
            needed = max(needed, REGIME_CONFIG.min_bars + 1)
        requirements[timeframe] = max(requirements.get(timeframe, 0), needed)
    return requirements


def _symbol_key(symbol: str) -> str:
    return canonical_symbol(symbol)


@dataclass(frozen=True, slots=True)
class PrerequisiteState:
    symbol: str
    timeframe: str
    required_bars: int
    available_bars: int
    expected_close: str
    latest_close: str | None
    ready: bool
    reason: str
    issues: tuple[str, ...]
    missing_bars: int
    lag_seconds: int | None
    gap_count: int
    first_gap_open: str | None
    invalid_exact_volume_bars: int


@dataclass(frozen=True, slots=True)
class ScannerPrerequisiteReport:
    generated_at: str
    exchange: str
    ready: bool
    rows: tuple[PrerequisiteState, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "exchange": self.exchange,
            "ready": self.ready,
            "rows": [asdict(row) for row in self.rows],
        }


def _validate_tail(
    candles: Sequence[Candle],
    *,
    symbol: str,
    timeframe: str,
    required_bars: int,
    now: datetime,
) -> PrerequisiteState:
    expected_close = floor_time(now, timeframe)
    ordered = sorted(candles, key=lambda candle: candle.open_time)
    tail = ordered[-required_bars:]
    latest_close = tail[-1].close_time if tail else None
    missing_bars = max(0, required_bars - len(tail))
    lag_seconds = (
        max(0, int((expected_close - latest_close).total_seconds()))
        if latest_close is not None
        else None
    )
    step = timedelta(seconds=TF_SECONDS[timeframe])
    gap_count = 0
    first_gap_open: datetime | None = None
    for previous, current in pairwise(tail):
        delta = current.open_time - previous.open_time
        if delta == step:
            continue
        if first_gap_open is None:
            first_gap_open = previous.open_time + step
        # A misaligned or reversed timestamp is one integrity defect. For a
        # normal forward hole, report the exact number of absent buckets.
        ratio, remainder = divmod(delta.total_seconds(), step.total_seconds())
        gap_count += max(1, int(ratio) - 1) if remainder == 0 else 1
    invalid_exact_volume_bars = sum(
        1
        for candle in tail
        if (
            not candle.is_closed
            or candle.volume <= 0
            or candle.quote_volume <= 0
            or candle.trade_count <= 0
            or candle.vwap is None
        )
    )
    issues: list[str] = []
    if missing_bars:
        issues.append("insufficient_history")
    if latest_close != expected_close:
        issues.append("stale_tail")
    if gap_count:
        issues.append("non_contiguous")
    if invalid_exact_volume_bars:
        issues.append("non_exact_volume")
    reason = issues[0] if issues else "ok"

    return PrerequisiteState(
        symbol=symbol,
        timeframe=timeframe,
        required_bars=required_bars,
        available_bars=len(tail),
        expected_close=expected_close.isoformat(),
        latest_close=latest_close.isoformat() if latest_close is not None else None,
        ready=reason == "ok",
        reason=reason,
        issues=tuple(issues),
        missing_bars=missing_bars,
        lag_seconds=lag_seconds,
        gap_count=gap_count,
        first_gap_open=(first_gap_open.isoformat() if first_gap_open else None),
        invalid_exact_volume_bars=invalid_exact_volume_bars,
    )


def scanner_prerequisites(
    candle_root: Path | str,
    *,
    exchange: str,
    symbols: Sequence[str],
    requirements: Mapping[str, int] = DEFAULT_REQUIREMENTS,
    now: datetime | None = None,
) -> ScannerPrerequisiteReport:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    moment = moment.astimezone(UTC)
    store = CandleParquetStore(candle_root, exchange=exchange)
    rows = tuple(
        _validate_tail(
            store.read(_symbol_key(symbol), timeframe),
            symbol=_symbol_key(symbol),
            timeframe=timeframe,
            required_bars=required_bars,
            now=moment,
        )
        for symbol in symbols
        for timeframe, required_bars in requirements.items()
    )
    return ScannerPrerequisiteReport(
        generated_at=moment.isoformat(),
        exchange=exchange,
        ready=all(row.ready for row in rows),
        rows=rows,
    )


def _write_report(path: Path, report: ScannerPrerequisiteReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    os.replace(temporary, path)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="verify exact-volume scanner candle prerequisites"
    )
    parser.add_argument("--candle-root", default="data/candles")
    parser.add_argument("--exchange", default="binanceusdm")
    parser.add_argument(
        "--symbols", default="BTC/USDT:USDT,ETH/USDT:USDT"
    )
    parser.add_argument(
        "--report", default="data/reports/scanner_prerequisites.json"
    )
    parser.add_argument(
        "--roster",
        help="derive required bars from this versioned shadow-observer roster",
    )
    args = parser.parse_args(argv)
    symbols = _csv(args.symbols)
    if not symbols:
        parser.error("--symbols must name at least one symbol")
    report = scanner_prerequisites(
        args.candle_root,
        exchange=args.exchange,
        symbols=symbols,
        requirements=(requirements_from_roster(args.roster) if args.roster else DEFAULT_REQUIREMENTS),
    )
    _write_report(Path(args.report), report)
    for row in report.rows:
        diagnostics = (
            f"missing={row.missing_bars} lag_s={row.lag_seconds} "
            f"gaps={row.gap_count} first_gap={row.first_gap_open or '-'} "
            f"invalid_exact={row.invalid_exact_volume_bars}"
        )
        print(
            f"{row.symbol} {row.timeframe}: "
            f"{row.available_bars}/{row.required_bars} {row.reason} {diagnostics}"
        )
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
