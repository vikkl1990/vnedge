"""Research-only alpha factory.

The factory is the layer that hunts for structural edge families before a
strategy exists. It does not place orders, promote lanes, or bless a signal.
It ranks hypotheses by conditional forward expectancy after realistic maker
costs, then queues the best ones for conservative tick replay.
"""

from __future__ import annotations

import argparse
import bisect
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Iterable, Literal

from vnedge.research.scalper_replay_diagnostics import ScalperReplayRow
from vnedge.research.scalper_scanners import (
    ExecutionRouteDecision,
    ScalperScannerConfig,
    decide_execution_route,
)
from vnedge.research.universe import ResearchTarget, load_research_targets
from vnedge.scalping.features import IncrementalFeatureEngine, ScalperFeatures
from vnedge.scalping.microstructure import TopOfBook
from vnedge.scalping.replay_backtester import load_tick_events


ALPHA_FACTORY_ID = "structural_alpha_factory_v1"
ALPHA_FACTORY_FLOW = (
    "record_tick_l2",
    "mine_structural_hypotheses",
    "score_after_cost",
    "route_policy",
    "conservative_replay_required",
    "untouched_judgment",
    "paper_shadow_after_human_approval",
)


@dataclass(frozen=True)
class AlphaFactoryConfig:
    scanner_config: ScalperScannerConfig = field(default_factory=ScalperScannerConfig)
    horizons_ms: tuple[int, ...] = (1_000, 3_000, 5_000, 15_000)
    sample_every_ms: int = 500
    min_samples: int = 30
    max_spread_bps: float = 3.0
    min_trade_count: int = 8
    min_abs_imbalance: float = 0.35
    flow_agreement: float = 0.66
    min_pressure_notional_usd: float = 75_000.0
    microprice_dislocation_bps: float = 0.20
    liquidity_vacuum_depth_usd: float = 250_000.0
    min_realized_vol_bps: float = 0.08
    maker_bps: float = 2.0
    taker_bps: float = 5.0
    slippage_bps: float = 1.0
    safety_buffer_bps: float = 1.0
    notional_usd: float = 100.0


@dataclass(frozen=True)
class AlphaObservation:
    ts_ms: int
    side: Literal["buy", "sell"]
    forward_bps: float
    net_bps: float


@dataclass(frozen=True)
class AlphaHypothesisResult:
    alpha_factory_id: str
    exchange: str
    symbol: str
    day: str
    hypothesis_id: str
    family: str
    side: str
    horizon_ms: int
    samples: int
    avg_forward_bps: float | None
    avg_net_bps: float | None
    win_rate_pct: float
    profit_factor: float | None
    route_decision: ExecutionRouteDecision
    state: str
    cost_bps: float
    replay_priority: float
    rationale: str
    evidence: dict
    can_trade: bool = False
    can_promote: bool = False
    requires_conservative_replay: bool = True
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _Point:
    ts_ms: int
    top: TopOfBook
    features: ScalperFeatures


@dataclass(frozen=True)
class _Signal:
    family: str
    side: Literal["buy", "sell"]
    rationale: str
    evidence: dict

    @property
    def label(self) -> str:
        return self.family


def alpha_factory_policy() -> dict:
    return {
        "status": "research_only",
        "can_trade": False,
        "can_promote": False,
        "requires_conservative_replay": True,
        "requires_untouched_judgment": True,
        "alpha_factory_id": ALPHA_FACTORY_ID,
        "principle": (
            "raw structural hypotheses are not signals; they must survive "
            "conservative tick replay, untouched judgment, and human approval"
        ),
        "families": [
            "forced_flow_continuation",
            "absorption_reversal",
            "microprice_dislocation",
            "liquidity_vacuum_continuation",
            "volatility_impulse",
        ],
    }


def run_alpha_factory(
    data_root: Path | str,
    targets: tuple[ResearchTarget, ...],
    *,
    days: tuple[str, ...],
    config: AlphaFactoryConfig = AlphaFactoryConfig(),
    max_rows: int = 50,
) -> dict:
    """Run structural alpha discovery over already recorded tick/L2 tape."""
    root = Path(data_root)
    hypotheses = mine_recorded_alpha_days(root, targets, days, config)
    replay_queue = [
        {**h.to_dict(), "source": "alpha_factory_structural_hypothesis"}
        for h in hypotheses
        if h.state in {"REPLAY_REQUIRED_MAKER", "REPLAY_REQUIRED_TAKER"}
    ][:max_rows]
    payload = {
        "policy": alpha_factory_policy(),
        "flow": list(ALPHA_FACTORY_FLOW),
        "flow_guards": {
            "raw_hypothesis_is_not_signal": True,
            "conservative_replay_required": True,
            "untouched_judgment_required": True,
            "human_approval_required": True,
            "can_trade": False,
            "can_promote": False,
        },
        "targets": [asdict(t) for t in targets],
        "days": list(days),
        "hypotheses": [h.to_dict() for h in hypotheses[:max_rows]],
        "replay_queue": replay_queue,
        "recorder_directives": _recorder_directives(targets, days, hypotheses),
    }
    if not days:
        payload["note"] = "no tick/L2 days supplied; record public tape before alpha mining"
    return payload


def mine_recorded_alpha_days(
    data_root: Path | str,
    targets: Iterable[ResearchTarget],
    days: Iterable[str],
    config: AlphaFactoryConfig = AlphaFactoryConfig(),
) -> tuple[AlphaHypothesisResult, ...]:
    out: list[AlphaHypothesisResult] = []
    for target in targets:
        for day in days:
            events = load_tick_events(data_root, target.exchange, target.symbol, day)
            out.extend(
                mine_structural_alpha_events(
                    events,
                    exchange=target.exchange,
                    symbol=target.symbol,
                    day=day,
                    config=config,
                )
            )
    return tuple(sorted(out, key=_result_sort_key))


def mine_structural_alpha_events(
    events: list[tuple[int, str, object]],
    *,
    exchange: str,
    symbol: str,
    day: str,
    config: AlphaFactoryConfig = AlphaFactoryConfig(),
) -> tuple[AlphaHypothesisResult, ...]:
    points = _feature_points(events)
    if len(points) < 2:
        return ()
    timestamps = [p.ts_ms for p in points]
    mids = [p.top.mid_price for p in points]
    observations: dict[tuple[str, str, int], list[AlphaObservation]] = {}
    evidence: dict[tuple[str, str, int], dict] = {}
    last_sample: dict[tuple[str, str, int], int] = {}

    for i, point in enumerate(points):
        for signal in _signals(point, config):
            for horizon_ms in config.horizons_ms:
                key = (signal.label, signal.side, horizon_ms)
                if point.ts_ms - last_sample.get(key, -10**15) < config.sample_every_ms:
                    continue
                j = bisect.bisect_left(timestamps, point.ts_ms + horizon_ms, lo=i + 1)
                if j >= len(points):
                    continue
                forward = (mids[j] - point.top.mid_price) / point.top.mid_price * 10_000.0
                signed = forward if signal.side == "buy" else -forward
                net = signed - _maker_replay_cost_bps(config)
                observations.setdefault(key, []).append(
                    AlphaObservation(point.ts_ms, signal.side, signed, net)
                )
                evidence.setdefault(key, signal.evidence)
                last_sample[key] = point.ts_ms

    results = [
        _result(exchange, symbol, day, key, obs, evidence.get(key, {}), config)
        for key, obs in observations.items()
    ]
    return tuple(sorted(results, key=_result_sort_key))


def render_alpha_report(results: Iterable[AlphaHypothesisResult], *, limit: int = 40) -> str:
    lines = [
        "alpha factory",
        "policy=research_only can_trade=false can_promote=false",
        "",
        "state                 route         side pf    net_bps fwd_bps win% samples "
        "priority exchange    symbol          horizon family",
    ]
    for r in tuple(results)[:limit]:
        lines.append(
            f"{r.state:<21} {r.route_decision.route:<13} {r.side:<4} "
            f"{_fmt(r.profit_factor):>5} {_fmt(r.avg_net_bps):>7} "
            f"{_fmt(r.avg_forward_bps):>7} {r.win_rate_pct:>4.0f} "
            f"{r.samples:>7} {r.replay_priority:>8.1f} {r.exchange:<11} "
            f"{r.symbol:<15} {r.horizon_ms:>7} {r.family}"
        )
    if len(lines) == 4:
        lines.append("no structural hypotheses found; record more tick/L2 data")
    return "\n".join(lines)


def _feature_points(events: list[tuple[int, str, object]]) -> list[_Point]:
    engine = IncrementalFeatureEngine(max_midpoints=240, max_trades=300)
    points: list[_Point] = []
    for ts_ms, kind, obj in events:
        if kind == "trade":
            engine.on_trade(obj)
        elif kind == "book" and isinstance(obj, TopOfBook):
            features = engine.on_book(obj)
            points.append(_Point(ts_ms, obj, features))
    return points


def _signals(point: _Point, config: AlphaFactoryConfig) -> tuple[_Signal, ...]:
    top = point.top
    features = point.features
    if (
        top.spread_bps > config.max_spread_bps
        or features.trade_count < config.min_trade_count
    ):
        return ()

    out: list[_Signal] = []
    imb = features.book_imbalance
    buy_ratio = features.taker_buy_ratio
    pressure = features.signed_trade_notional_usd
    micro_bps = (features.microprice - features.mid_price) / features.mid_price * 10_000.0
    evidence = {
        "spread_bps": round(top.spread_bps, 4),
        "imbalance": round(imb, 4),
        "taker_buy_ratio": round(buy_ratio, 4),
        "signed_trade_notional_usd": round(pressure, 2),
        "microprice_bps": round(micro_bps, 4),
        "top_depth_usd": round(top.top_depth_usd, 2),
        "realized_vol_bps": round(features.realized_vol_bps, 4),
    }

    if (
        pressure >= config.min_pressure_notional_usd
        and buy_ratio >= config.flow_agreement
        and imb >= config.min_abs_imbalance
    ):
        out.append(_Signal("forced_flow_continuation", "buy",
                           "aggressive buy flow agrees with bid-side pressure", evidence))
    if (
        pressure <= -config.min_pressure_notional_usd
        and buy_ratio <= 1.0 - config.flow_agreement
        and imb <= -config.min_abs_imbalance
    ):
        out.append(_Signal("forced_flow_continuation", "sell",
                           "aggressive sell flow agrees with ask-side pressure", evidence))

    if (
        pressure <= -config.min_pressure_notional_usd
        and buy_ratio <= 1.0 - config.flow_agreement
        and imb >= config.min_abs_imbalance
    ):
        out.append(_Signal("absorption_reversal", "buy",
                           "bid-side liquidity absorbs sell pressure", evidence))
    if (
        pressure >= config.min_pressure_notional_usd
        and buy_ratio >= config.flow_agreement
        and imb <= -config.min_abs_imbalance
    ):
        out.append(_Signal("absorption_reversal", "sell",
                           "ask-side liquidity absorbs buy pressure", evidence))

    if micro_bps >= config.microprice_dislocation_bps and imb > 0:
        out.append(_Signal("microprice_dislocation", "buy",
                           "microprice displaced above mid with supportive book", evidence))
    if micro_bps <= -config.microprice_dislocation_bps and imb < 0:
        out.append(_Signal("microprice_dislocation", "sell",
                           "microprice displaced below mid with supportive book", evidence))

    if (
        top.top_depth_usd <= config.liquidity_vacuum_depth_usd
        and features.realized_vol_bps >= config.min_realized_vol_bps
    ):
        if pressure >= config.min_pressure_notional_usd and buy_ratio >= config.flow_agreement:
            out.append(_Signal("liquidity_vacuum_continuation", "buy",
                               "thin touch plus aggressive buy impulse", evidence))
        if pressure <= -config.min_pressure_notional_usd and buy_ratio <= 1 - config.flow_agreement:
            out.append(_Signal("liquidity_vacuum_continuation", "sell",
                               "thin touch plus aggressive sell impulse", evidence))

    if features.realized_vol_bps >= config.min_realized_vol_bps * 2:
        if pressure >= config.min_pressure_notional_usd and buy_ratio >= config.flow_agreement:
            out.append(_Signal("volatility_impulse", "buy",
                               "volatility expansion with one-sided buy flow", evidence))
        if pressure <= -config.min_pressure_notional_usd and buy_ratio <= 1 - config.flow_agreement:
            out.append(_Signal("volatility_impulse", "sell",
                               "volatility expansion with one-sided sell flow", evidence))

    return tuple(out)


def _result(
    exchange: str,
    symbol: str,
    day: str,
    key: tuple[str, str, int],
    observations: list[AlphaObservation],
    evidence: dict,
    config: AlphaFactoryConfig,
) -> AlphaHypothesisResult:
    family, side, horizon_ms = key
    net = [o.net_bps for o in observations]
    forward = [o.forward_bps for o in observations]
    wins = [v for v in net if v > 0]
    losses = [-v for v in net if v < 0]
    pf = (sum(wins) / sum(losses)) if wins and losses else (999.0 if wins else None)
    avg_net = mean(net) if net else None
    avg_forward = mean(forward) if forward else None
    win_rate = len(wins) / len(net) * 100.0 if net else 0.0
    fake_row = ScalperReplayRow(
        min_imbalance=0.0,
        max_spread_bps=config.max_spread_bps,
        quotes=len(observations),
        filled=len(observations),
        missed=0,
        open_at_end=0,
        fill_rate_pct=100.0 if observations else 0.0,
        net_usd=sum(net) / 10_000.0 * config.notional_usd,
        avg_net_bps=avg_net,
        avg_adverse_bps=0.0 if observations else None,
        verdict="CANDIDATE" if avg_net and avg_net > 0 else "NEGATIVE_EDGE",
        profit_factor=pf,
        breakeven_bps=_maker_replay_cost_bps(config),
    )
    route = decide_execution_route(fake_row, config.scanner_config)
    state = _alpha_state(len(observations), route, config)
    return AlphaHypothesisResult(
        alpha_factory_id=ALPHA_FACTORY_ID,
        exchange=exchange,
        symbol=symbol,
        day=day,
        hypothesis_id=f"{family}|side={side}|h={horizon_ms}",
        family=family,
        side=side,
        horizon_ms=horizon_ms,
        samples=len(observations),
        avg_forward_bps=avg_forward,
        avg_net_bps=avg_net,
        win_rate_pct=win_rate,
        profit_factor=pf,
        route_decision=route,
        state=state,
        cost_bps=_maker_replay_cost_bps(config),
        replay_priority=_replay_priority(len(observations), avg_net, pf, route, config),
        rationale=_rationale(family, route, avg_net),
        evidence=evidence,
    )


def _alpha_state(
    samples: int,
    route: ExecutionRouteDecision,
    config: AlphaFactoryConfig,
) -> str:
    if samples < config.min_samples:
        return "UNDER_SAMPLED"
    if route.route == "TAKER_ALLOWED":
        return "REPLAY_REQUIRED_TAKER"
    if route.route == "MAKER_ONLY":
        return "REPLAY_REQUIRED_MAKER"
    return "BELOW_COST"


def _replay_priority(
    samples: int,
    avg_net: float | None,
    pf: float | None,
    route: ExecutionRouteDecision,
    config: AlphaFactoryConfig,
) -> float:
    sample_score = min(samples / max(config.min_samples, 1), 3.0) * 15.0
    net_score = max(avg_net or 0.0, 0.0) * 6.0
    pf_score = min(pf or 0.0, 5.0) * 8.0
    route_bonus = 0.0
    if route.route == "TAKER_ALLOWED":
        route_bonus = 20.0
    elif route.route == "MAKER_ONLY":
        route_bonus = 10.0
    return round(sample_score + net_score + pf_score + route_bonus, 2)


def _rationale(family: str, route: ExecutionRouteDecision, avg_net: float | None) -> str:
    if route.route == "BLOCKED":
        return f"{family} does not clear maker cost/PF floor yet"
    return (
        f"{family} has positive conditional expectancy after maker-first costs "
        f"({_fmt(avg_net)}bps avg net); route={route.route}; replay before promotion"
    )


def _maker_replay_cost_bps(config: AlphaFactoryConfig) -> float:
    return config.maker_bps + config.taker_bps + config.slippage_bps + config.safety_buffer_bps


def _recorder_directives(
    targets: tuple[ResearchTarget, ...],
    days: tuple[str, ...],
    hypotheses: tuple[AlphaHypothesisResult, ...],
) -> list[dict]:
    if not days:
        return [
            {
                "exchange": t.exchange,
                "symbol": t.symbol,
                "reason": "no recorded tick/L2 day available",
                "priority": 100.0,
            }
            for t in targets
        ]
    if not hypotheses:
        return [
            {
                "exchange": t.exchange,
                "symbol": t.symbol,
                "reason": "record more tape; no structural hypothesis fired",
                "priority": 60.0,
            }
            for t in targets
        ]
    under_sampled = [h for h in hypotheses if h.state == "UNDER_SAMPLED"]
    return [
        {
            "exchange": h.exchange,
            "symbol": h.symbol,
            "reason": f"{h.family} fired but sample count is thin",
            "priority": min(100.0, 50.0 + h.samples),
        }
        for h in under_sampled[:12]
    ]


def _result_sort_key(r: AlphaHypothesisResult) -> tuple[int, float, float, int, str]:
    state_rank = {
        "REPLAY_REQUIRED_TAKER": 0,
        "REPLAY_REQUIRED_MAKER": 1,
        "UNDER_SAMPLED": 2,
        "BELOW_COST": 3,
    }.get(r.state, 4)
    return (
        state_rank,
        -r.replay_priority,
        -(r.avg_net_bps or -999.0),
        -r.samples,
        r.hypothesis_id,
    )


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _fmt(v: float | None, digits: int = 2) -> str:
    return "--" if v is None else f"{v:.{digits}f}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="mine structural alpha hypotheses")
    p.add_argument("--data-root", default="data")
    p.add_argument("--days", required=True, help="comma-separated UTC days, YYYYMMDD")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    targets = load_research_targets()
    results = mine_recorded_alpha_days(
        args.data_root,
        targets,
        _split_csv(args.days),
    )
    if args.json:
        print(json.dumps([r.to_dict() for r in results[:args.limit]], indent=2))
    else:
        print(render_alpha_report(results, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
