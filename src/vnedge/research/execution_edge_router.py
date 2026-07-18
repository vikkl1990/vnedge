"""Research-only opportunity router for scanner events.

The current scanner stack detects visual or structural setups. This module asks
the executable question: if that event had been routed as maker-first or taker,
did it clear the fee wall with enough margin to be worth further study?

The router is a truth/backtest layer, not a live decision model. Route choice
uses ex-ante event fields only. Forward labels are used after the choice to
score what happened. Every payload is therefore explicitly `can_trade=false`
and `can_promote=false`.
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Iterable, Literal

import pandas as pd

from vnedge.data.parquet_store import ParquetStore
from vnedge.research.execution_edge_labeler import (
    EdgeLabelerConfig,
    EventTruthLabel,
    SignalEvent,
    label_events,
    strategy_signal_events,
)
from vnedge.scalping.parameter_registry import (
    DEFAULT_SCALPER_PARAMETER_REGISTRY,
    ExchangeFeeProfile,
)
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.strategy_registry import get_strategy_class

RouteAction = Literal["SKIP", "MAKER", "MAKER_THEN_TAKER", "TAKER_NOW"]
RouterVerdict = Literal[
    "NO_OPPORTUNITIES",
    "UNDER_SAMPLED",
    "NEGATIVE_AFTER_COST",
    "MAKER_EDGE",
    "TAKER_EDGE",
    "MIXED_ROUTE_EDGE",
]

DEFAULT_SCALPER_STRATEGIES = (
    "sats_5m_scalper_v1",
    "stealth_trail_bbp_v1",
    "human_trade_fingerprint_v1",
    "luxy_ut_bot_forecast_v1",
    "momentum_cascade_lyro_v1",
    "luxara_live_plan_qtm_v1",
    "luxara_break_bounce_v27_v1",
    "smc_playbook_scalper_v1",
    "quant_signal_pack_v1",
    "alpha_stack_confluence_v1",
)


@dataclass(frozen=True)
class OpportunityRouterConfig:
    """Route-gate defaults for scalper opportunity truth reports."""

    horizon_bars: int = 12
    min_samples: int = 20
    min_expected_net_edge_bps: float = 25.0
    min_profit_factor: float = 1.50
    maker_fill_probability: float = 0.60
    maker_fill_floor: float = 0.50
    maker_fallback_fill_floor: float = 0.25
    taker_extra_buffer_bps: float = 5.0
    default_lookback_days: int = 30

    def __post_init__(self) -> None:
        if self.horizon_bars < 1:
            raise ValueError("horizon_bars must be >= 1")
        if self.min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        for name, value in (
            ("maker_fill_probability", self.maker_fill_probability),
            ("maker_fill_floor", self.maker_fill_floor),
            ("maker_fallback_fill_floor", self.maker_fallback_fill_floor),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.min_expected_net_edge_bps < 0:
            raise ValueError("min_expected_net_edge_bps cannot be negative")
        if self.min_profit_factor < 1.0:
            raise ValueError("min_profit_factor must be >= 1")
        if self.taker_extra_buffer_bps < 0:
            raise ValueError("taker_extra_buffer_bps cannot be negative")


@dataclass(frozen=True)
class OpportunityRoute:
    """One opportunity labeled through maker and taker routes."""

    event_id: str
    ts: str
    side: str
    source_id: str
    strategy_id: str
    action: RouteAction
    reason: str
    selected_route: str | None
    selected_net_bps: float | None
    selected_gross_bps: float | None
    selected_cost_bps: float | None
    maker_net_bps: float | None
    maker_gross_bps: float | None
    maker_cost_bps: float
    taker_net_bps: float | None
    taker_gross_bps: float | None
    taker_cost_bps: float
    maker_fill_probability: float
    expected_edge_bps: float | None
    outcome: str
    mfe_bps: float | None
    mae_bps: float | None
    risk_bps: float | None
    metadata: dict = field(default_factory=dict)
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True
    decision_uses_forward_truth: bool = False

    @property
    def routed(self) -> bool:
        return self.action != "SKIP" and self.selected_net_bps is not None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OpportunityRouterSummary:
    verdict: RouterVerdict
    opportunities: int
    routed: int
    skipped: int
    action_counts: dict[str, int]
    avg_selected_net_bps: float | None
    avg_selected_gross_bps: float | None
    profit_factor: float | None
    win_rate_pct: float
    avg_mfe_bps: float | None
    avg_mae_bps: float | None
    primary_blocker: str
    paper_candidate: bool
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def label_strategy_opportunities(
    candles: pd.DataFrame,
    strategy: BaseStrategy,
    *,
    exchange: str = "binanceusdm",
    config: OpportunityRouterConfig = OpportunityRouterConfig(),
    fee_profile: ExchangeFeeProfile | None = None,
) -> tuple[OpportunityRoute, ...]:
    """Build maker/taker labels and route every raw strategy opportunity."""

    base_events = strategy_signal_events(candles, strategy, route="MAKER_ONLY")
    return label_opportunities(
        candles,
        base_events,
        exchange=exchange,
        config=config,
        fee_profile=fee_profile,
    )


def label_opportunities(
    candles: pd.DataFrame,
    events: Iterable[SignalEvent],
    *,
    exchange: str = "binanceusdm",
    config: OpportunityRouterConfig = OpportunityRouterConfig(),
    fee_profile: ExchangeFeeProfile | None = None,
) -> tuple[OpportunityRoute, ...]:
    """Label explicit events through both execution routes and classify action."""

    raw_events = tuple(events)
    if not raw_events:
        return ()
    fees = fee_profile or DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(exchange)
    maker_events = tuple(
        replace(
            event,
            route="MAKER_ONLY",
            fill_probability=(
                event.fill_probability
                if event.fill_probability is not None
                else config.maker_fill_probability
            ),
        )
        for event in raw_events
    )
    taker_events = tuple(replace(event, route="TAKER_ALLOWED") for event in raw_events)
    label_config = EdgeLabelerConfig(
        horizon_bars=config.horizon_bars,
        min_samples=config.min_samples,
        min_avg_net_bps=config.min_expected_net_edge_bps,
        min_profit_factor=config.min_profit_factor,
        taker_min_avg_net_bps=(
            config.min_expected_net_edge_bps + config.taker_extra_buffer_bps
        ),
        taker_min_profit_factor=config.min_profit_factor,
        default_maker_fill_probability=config.maker_fill_probability,
        min_maker_fill_probability=config.maker_fill_floor,
    )
    maker_labels = label_events(
        candles,
        maker_events,
        exchange=exchange,
        config=label_config,
        fee_profile=fees,
    )
    taker_labels = label_events(
        candles,
        taker_events,
        exchange=exchange,
        config=label_config,
        fee_profile=fees,
    )
    return tuple(
        route_opportunity(maker, taker, config=config)
        for maker, taker in zip(maker_labels, taker_labels, strict=True)
    )


def route_opportunity(
    maker: EventTruthLabel,
    taker: EventTruthLabel,
    *,
    config: OpportunityRouterConfig = OpportunityRouterConfig(),
) -> OpportunityRoute:
    """Choose a route from ex-ante event fields, then attach truth outcome."""

    expected_edge_bps = _expected_edge_bps(maker)
    non_fill_blockers = tuple(
        blocker for blocker in maker.blockers
        if blocker != "maker_fill_probability_below_floor"
    )
    maker_ok = (
        not non_fill_blockers
        and maker.fill_probability >= config.maker_fill_floor
        and (
            expected_edge_bps is None
            or expected_edge_bps >= config.min_expected_net_edge_bps
        )
    )
    taker_threshold = config.min_expected_net_edge_bps + config.taker_extra_buffer_bps
    taker_ok = (
        expected_edge_bps is not None
        and expected_edge_bps >= taker_threshold
        and not taker.blockers
    )
    fallback_ok = (
        expected_edge_bps is not None
        and expected_edge_bps >= taker_threshold
        and maker.fill_probability >= config.maker_fallback_fill_floor
        and taker_ok
    )

    if maker_ok:
        action: RouteAction = "MAKER"
        reason = (
            "maker baseline routed without edge forecast"
            if expected_edge_bps is None
            else "maker route clears ex-ante edge and fill floor"
        )
        selected_route = "MAKER_ONLY"
        selected = maker
    elif fallback_ok:
        action = "MAKER_THEN_TAKER"
        reason = "maker edge exists but fill confidence needs taker fallback"
        selected_route = "MAKER_THEN_TAKER"
        selected = taker
    elif taker_ok:
        action = "TAKER_NOW"
        reason = "taker route clears fee wall plus safety buffer"
        selected_route = "TAKER_ALLOWED"
        selected = taker
    else:
        action = "SKIP"
        reason = _skip_reason(maker, taker, expected_edge_bps, config)
        selected_route = None
        selected = None

    return OpportunityRoute(
        event_id=maker.event_id,
        ts=maker.ts,
        side=maker.side,
        source_id=maker.source_id,
        strategy_id=maker.strategy_id,
        action=action,
        reason=reason,
        selected_route=selected_route,
        selected_net_bps=selected.net_bps if selected is not None else None,
        selected_gross_bps=selected.gross_bps if selected is not None else None,
        selected_cost_bps=selected.route_cost_bps if selected is not None else None,
        maker_net_bps=maker.net_bps,
        maker_gross_bps=maker.gross_bps,
        maker_cost_bps=maker.route_cost_bps,
        taker_net_bps=taker.net_bps,
        taker_gross_bps=taker.gross_bps,
        taker_cost_bps=taker.route_cost_bps,
        maker_fill_probability=maker.fill_probability,
        expected_edge_bps=expected_edge_bps,
        outcome=selected.outcome if selected is not None else _dominant_outcome(maker, taker),
        mfe_bps=selected.mfe_bps if selected is not None else maker.mfe_bps,
        mae_bps=selected.mae_bps if selected is not None else maker.mae_bps,
        risk_bps=selected.risk_bps if selected is not None else maker.risk_bps,
        metadata=dict(maker.metadata),
    )


def summarize_routes(
    opportunities: Iterable[OpportunityRoute],
    *,
    config: OpportunityRouterConfig = OpportunityRouterConfig(),
) -> OpportunityRouterSummary:
    rows = tuple(opportunities)
    routed = [row for row in rows if row.routed and row.selected_net_bps is not None]
    net = [float(row.selected_net_bps) for row in routed if row.selected_net_bps is not None]
    gross = [
        float(row.selected_gross_bps)
        for row in routed
        if row.selected_gross_bps is not None
    ]
    wins = [v for v in net if v > 0]
    losses = [-v for v in net if v < 0]
    pf = (sum(wins) / sum(losses)) if wins and losses else (999.0 if wins else None)
    avg_net = mean(net) if net else None
    mfe = [float(row.mfe_bps) for row in routed if row.mfe_bps is not None]
    mae = [float(row.mae_bps) for row in routed if row.mae_bps is not None]
    action_counts = dict(Counter(row.action for row in rows))
    verdict, blocker = _summary_verdict(rows, routed, avg_net, pf, config)
    paper_candidate = verdict in {"MAKER_EDGE", "TAKER_EDGE", "MIXED_ROUTE_EDGE"}
    return OpportunityRouterSummary(
        verdict=verdict,
        opportunities=len(rows),
        routed=len(routed),
        skipped=len(rows) - len(routed),
        action_counts=action_counts,
        avg_selected_net_bps=_round_or_none(avg_net),
        avg_selected_gross_bps=_round_or_none(mean(gross) if gross else None),
        profit_factor=_round_or_none(pf),
        win_rate_pct=round(len(wins) / len(net) * 100.0, 2) if net else 0.0,
        avg_mfe_bps=_round_or_none(mean(mfe) if mfe else None),
        avg_mae_bps=_round_or_none(mean(mae) if mae else None),
        primary_blocker=blocker,
        paper_candidate=paper_candidate,
    )


def build_router_report(
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    strategy_id: str,
    opportunities: Iterable[OpportunityRoute],
    config: OpportunityRouterConfig = OpportunityRouterConfig(),
) -> dict:
    rows = tuple(opportunities)
    summary = summarize_routes(rows, config=config)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "truth_layer": "execution_edge_router_v1",
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": strategy_id,
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "requires_untouched_judgment": True,
            "decision_uses_forward_truth": False,
            "operator_note": (
                "This report labels opportunities for edge-model training and "
                "scanner triage. It is not a live trading decision model."
            ),
        },
        "config": asdict(config),
        "summary": summary.to_dict(),
        "opportunities": [row.to_dict() for row in rows],
    }


def run_strategy_router_backtest(
    candles: pd.DataFrame,
    strategy: BaseStrategy,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    config: OpportunityRouterConfig = OpportunityRouterConfig(),
    fee_profile: ExchangeFeeProfile | None = None,
) -> dict:
    opportunities = label_strategy_opportunities(
        candles,
        strategy,
        exchange=exchange,
        config=config,
        fee_profile=fee_profile,
    )
    return build_router_report(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        strategy_id=strategy.strategy_id,
        opportunities=opportunities,
        config=config,
    )


def _skip_reason(
    maker: EventTruthLabel,
    taker: EventTruthLabel,
    expected_edge_bps: float | None,
    config: OpportunityRouterConfig,
) -> str:
    blockers = tuple(dict.fromkeys((*maker.blockers, *taker.blockers)))
    hard_blockers = tuple(
        blocker for blocker in blockers
        if blocker != "maker_fill_probability_below_floor"
    )
    if hard_blockers:
        return "blocked: " + ",".join(hard_blockers)
    if expected_edge_bps is not None and expected_edge_bps < config.min_expected_net_edge_bps:
        return "ex-ante expected edge below floor"
    if maker.fill_probability < config.maker_fallback_fill_floor:
        return "maker fill probability too low for fallback"
    if expected_edge_bps is None:
        return "no ex-ante edge forecast and maker fill confidence is low"
    if expected_edge_bps < (config.min_expected_net_edge_bps + config.taker_extra_buffer_bps):
        return "ex-ante edge does not clear taker fallback buffer"
    return "route policy rejected opportunity"


def _expected_edge_bps(label: EventTruthLabel) -> float | None:
    raw = label.metadata.get("expected_edge_bps")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _dominant_outcome(maker: EventTruthLabel, taker: EventTruthLabel) -> str:
    if taker.net_bps is not None and maker.net_bps is not None:
        return taker.outcome if taker.net_bps > maker.net_bps else maker.outcome
    return maker.outcome if maker.net_bps is not None else taker.outcome


def _summary_verdict(
    rows: tuple[OpportunityRoute, ...],
    routed: list[OpportunityRoute],
    avg_net: float | None,
    pf: float | None,
    config: OpportunityRouterConfig,
) -> tuple[RouterVerdict, str]:
    if not rows:
        return "NO_OPPORTUNITIES", "scanner produced no raw opportunities"
    if len(routed) < config.min_samples:
        return "UNDER_SAMPLED", f"only {len(routed)} routed events; need >= {config.min_samples}"
    if avg_net is None or avg_net < config.min_expected_net_edge_bps:
        return "NEGATIVE_AFTER_COST", "average selected net edge below floor"
    if (pf or 0.0) < config.min_profit_factor:
        return "NEGATIVE_AFTER_COST", "profit factor below route gate"
    actions = {row.action for row in routed}
    if actions == {"MAKER"}:
        return "MAKER_EDGE", "maker-first opportunities clear edge gate"
    if actions == {"TAKER_NOW"}:
        return "TAKER_EDGE", "taker opportunities clear fee wall"
    return "MIXED_ROUTE_EDGE", "maker/taker route mix clears edge gate"


def _round_or_none(value: float | None) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), 4)


def _window(df: pd.DataFrame, lookback_days: int | None) -> pd.DataFrame:
    if lookback_days is None or lookback_days <= 0 or df.empty:
        return df.reset_index(drop=True)
    ts = pd.to_datetime(df["timestamp"], utc=True)
    cutoff = pd.Timestamp(ts.iloc[-1]) - pd.Timedelta(days=lookback_days)
    return df[ts >= cutoff].reset_index(drop=True)


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _instantiate_strategy(
    strategy_cls: type[BaseStrategy],
    store: ParquetStore,
    exchange: str,
    symbol: str,
) -> BaseStrategy:
    params = inspect.signature(strategy_cls).parameters
    if "funding" not in params:
        return strategy_cls()
    try:
        funding = store.read_funding(exchange, symbol)
    except FileNotFoundError:
        funding = None
    if funding is None and params["funding"].default is inspect.Signature.empty:
        raise FileNotFoundError(
            f"{strategy_cls.strategy_id} requires funding data for {exchange}:{symbol}"
        )
    return strategy_cls(funding=funding)


def _render_table(reports: Iterable[dict]) -> str:
    reports = tuple(reports)
    lines = [
        "execution edge router backtest",
        "policy=research_only can_trade=false can_promote=false",
        "",
        "verdict              routed/opp pf    avg_net win% actions                         strategy",
    ]
    for report in reports:
        s = report["summary"]
        actions = ",".join(f"{k}:{v}" for k, v in sorted(s["action_counts"].items()))
        lines.append(
            f"{s['verdict']:<20} {s['routed']:>4}/{s['opportunities']:<4} "
            f"{_fmt(s['profit_factor']):>5} {_fmt(s['avg_selected_net_bps']):>8} "
            f"{s['win_rate_pct']:>4.0f} {actions:<31} {report['strategy']}"
        )
    if not reports:
        lines.append("no reports produced; check candles, symbols, and strategy ids")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "--" if value is None else f"{value:.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="backtest scanner opportunities through a cost-aware edge router"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument(
        "--strategies",
        default=",".join(DEFAULT_SCALPER_STRATEGIES),
        help="comma-separated strategy ids",
    )
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--horizon-bars", type=int, default=12)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--min-edge-bps", type=float, default=25.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.5)
    parser.add_argument("--maker-fill-probability", type=float, default=0.60)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args(argv)

    store = ParquetStore(args.data_root)
    candles = _window(
        store.read_candles(args.exchange, args.symbol, args.timeframe),
        args.lookback_days,
    )
    config = OpportunityRouterConfig(
        horizon_bars=args.horizon_bars,
        min_samples=args.min_samples,
        min_expected_net_edge_bps=args.min_edge_bps,
        min_profit_factor=args.min_profit_factor,
        maker_fill_probability=args.maker_fill_probability,
        default_lookback_days=args.lookback_days,
    )
    reports: list[dict] = []
    for strategy_id in _split_csv(args.strategies):
        strategy_cls = get_strategy_class(strategy_id)
        strategy = _instantiate_strategy(strategy_cls, store, args.exchange, args.symbol)
        reports.append(
            run_strategy_router_backtest(
                candles,
                strategy,
                exchange=args.exchange,
                symbol=args.symbol,
                timeframe=args.timeframe,
                config=config,
            )
        )
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "truth_layer": "execution_edge_router_v1",
        "exchange": args.exchange,
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "lookback_days": args.lookback_days,
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "requires_untouched_judgment": True,
        },
        "reports": reports,
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_render_table(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
