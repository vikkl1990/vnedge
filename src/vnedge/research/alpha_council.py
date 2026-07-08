"""Alpha Council: deterministic agent debate over research candidates.

Inspired by agent-native trading platforms, but intentionally scoped to VNEDGE's
promotion ladder. The council never trades and never promotes. It reads existing
research artifacts, lets specialized deterministic agents argue for/against each
candidate, and publishes a ranked next-action queue.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

COUNCIL_ID = "alpha_agent_council_v1"
DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_LATEST = DEFAULT_RESEARCH_DIR / "alpha_council_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "alpha_council_feed.jsonl"


@dataclass(frozen=True)
class AlphaCandidate:
    candidate_id: str
    source: str
    family: str
    exchange: str
    symbol: str
    timeframe: str
    state: str
    route_decision: str
    metrics: dict[str, Any]
    evidence: dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AgentOpinion:
    agent_id: str
    stance: str
    score_delta: float
    argument: str
    vetoes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def run_alpha_council(
    research_dir: Path | str = DEFAULT_RESEARCH_DIR,
    *,
    max_candidates: int = 30,
) -> dict:
    research = Path(research_dir)
    candidates = collect_candidates(research, max_candidates=max_candidates)
    debates = [debate_candidate(candidate) for candidate in candidates]
    debates.sort(key=lambda row: (-row["priority_score"], row["candidate"]["candidate_id"]))

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "council_id": COUNCIL_ID,
        "mode": "research_only_agent_debate",
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "guardrails": {
            "orders_allowed": False,
            "auto_promotion_allowed": False,
            "requires_gateway_for_any_future_order": True,
            "requires_untouched_judgment": True,
            "requires_replay_for_microstructure": True,
            "principle": (
                "agents may rank hypotheses, but only governed replay, untouched "
                "judgment, shadow, and paper trials can move a lane toward trading"
            ),
        },
        "summary": _summary(debates, candidates),
        "agent_roster": [
            {
                "agent_id": "edge_advocate",
                "role": "argues for statistically useful edge evidence",
            },
            {
                "agent_id": "skeptic",
                "role": "attacks sample size, overfit, instability, and weak gates",
            },
            {
                "agent_id": "execution_specialist",
                "role": "checks fee wall, maker/taker route, and fill assumptions",
            },
            {
                "agent_id": "risk_governor",
                "role": "applies promotion-ladder vetoes and capital-safety invariants",
            },
            {
                "agent_id": "research_director",
                "role": "turns the debate into the next falsifiable experiment",
            },
        ],
        "debates": debates,
    }


def collect_candidates(
    research_dir: Path | str,
    *,
    max_candidates: int = 30,
) -> list[AlphaCandidate]:
    research = Path(research_dir)
    candidates: list[AlphaCandidate] = []
    candidates.extend(_candle_candidates(_read_json(research / "latest.json")))
    candidates.extend(_event_leadlag_candidates(_read_json(research / "event_leadlag_latest.json")))
    candidates.extend(_fast_l2_candidates(_read_json(research / "l2_scout_latest.json")))
    candidates.extend(_l2_loop_candidates(_read_json(research / "l2_latest.json")))
    candidates.extend(_daily_scalper_candidates(_read_json(research / "daily_scalper_latest.json")))
    candidates.extend(_alpha_distillation_candidates(
        _read_json(research / "alpha_distillation_latest.json")
    ))
    return _dedupe_candidates(candidates)[:max_candidates]


def debate_candidate(candidate: AlphaCandidate) -> dict:
    opinions = (
        _edge_advocate(candidate),
        _skeptic(candidate),
        _execution_specialist(candidate),
        _risk_governor(candidate),
        _research_director(candidate),
    )
    vetoes = tuple(sorted({veto for opinion in opinions for veto in opinion.vetoes}))
    priority = _clamp(50.0 + sum(opinion.score_delta for opinion in opinions), 0.0, 100.0)
    next_action = _next_action(candidate, vetoes, priority)
    return {
        "candidate": candidate.to_dict(),
        "priority_score": round(priority, 2),
        "council_verdict": _verdict(priority, vetoes),
        "next_action": next_action,
        "can_trade": False,
        "can_promote": False,
        "vetoes": list(vetoes),
        "debate": [opinion.to_dict() for opinion in opinions],
    }


def _candle_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    for row in (payload or {}).get("results", []):
        verdict = str(row.get("verdict", "UNKNOWN"))
        net = _num(row.get("oos_net_usd"))
        pf = _num(row.get("profit_factor"))
        trades = _num(row.get("oos_trades"))
        has_positive_family = any(
            _num(f.get("net_usd")) > 0 and _num(f.get("profit_factor")) >= 1.1
            for f in (row.get("family_attribution") or {}).values()
            if isinstance(f, dict)
        )
        if verdict != "PASS" and net <= 0 and pf < 1.15 and not has_positive_family:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        strategy = str(row.get("strategy", "unknown_strategy"))
        timeframe = str(row.get("timeframe", "unknown"))
        out.append(AlphaCandidate(
            candidate_id=f"candle|{exchange}|{symbol}|{timeframe}|{strategy}",
            source="rolling_walk_forward",
            family=strategy,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            state=verdict,
            route_decision="CANDLE_RESEARCH",
            metrics={
                "oos_net_usd": net,
                "oos_trades": trades,
                "profit_factor": pf,
                "payoff_ratio": _num(row.get("payoff_ratio")),
                "profitable_windows_pct": _num(row.get("profitable_windows_pct")),
                "total_fees_usd": _num(row.get("total_fees_usd")),
            },
            evidence=row,
        ))
    return out


def _event_leadlag_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    for row in (payload or {}).get("hypotheses", []):
        state = str(row.get("state", "UNKNOWN"))
        route = _route_label(row.get("route_decision"))
        maker_net = _num(row.get("maker_avg_net_bps"))
        maker_pf = _num(row.get("maker_profit_factor"))
        if not state.startswith("EDGE_CANDIDATE") and maker_net <= 0 and maker_pf < 1.15:
            continue
        exchange = str(row.get("follower_exchange", "unknown"))
        symbol = str(row.get("follower_symbol") or row.get("base_asset", "unknown"))
        horizon = row.get("horizon_min", "?")
        source_id = str(row.get("hypothesis_id") or f"{exchange}|{symbol}|{horizon}")
        out.append(AlphaCandidate(
            candidate_id=f"event_leadlag|{source_id}",
            source="event_leadlag_alpha",
            family="cross_venue_event_leadlag_v1",
            exchange=exchange,
            symbol=symbol,
            timeframe=f"{horizon}m",
            state=state,
            route_decision=route,
            metrics={
                "samples": _num(row.get("samples")),
                "maker_avg_net_bps": maker_net,
                "taker_avg_net_bps": _num(row.get("taker_avg_net_bps")),
                "maker_profit_factor": maker_pf,
                "taker_profit_factor": _num(row.get("taker_profit_factor")),
                "win_rate_pct": _num(row.get("win_rate_pct")),
            },
            evidence=row,
        ))
    return out


def _fast_l2_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    for row in (payload or {}).get("top_results", []):
        state = str(row.get("state", "UNKNOWN"))
        route = _route_label(row.get("route_decision"))
        avg_net = _num(row.get("avg_net_bps") or row.get("maker_avg_net_bps"))
        pf = _num(row.get("profit_factor") or row.get("maker_profit_factor"))
        if "EDGE_CANDIDATE" not in state and avg_net <= 0 and pf < 1.15:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        family = str(row.get("family") or row.get("family_id") or row.get("strategy", "l2_scout"))
        out.append(AlphaCandidate(
            candidate_id=f"l2_scout|{exchange}|{symbol}|{family}|{state}",
            source="fast_l2_scout",
            family=family,
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("horizon_ms", "?")) + "ms",
            state=state,
            route_decision=route,
            metrics={
                "samples": _num(row.get("samples")),
                "avg_forward_bps": _num(row.get("avg_forward_bps")),
                "avg_net_bps": avg_net,
                "profit_factor": pf,
            },
            evidence=row,
        ))
    return out


def _l2_loop_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    scalper = (payload or {}).get("scalper_research") or {}
    for row in scalper.get("edge_hypotheses", []):
        if not isinstance(row, dict):
            continue
        state = str(row.get("state", "UNKNOWN"))
        route = _route_label(row.get("route_decision"))
        avg_net = _num(row.get("avg_net_bps") or row.get("maker_avg_net_bps"))
        if "EDGE_CANDIDATE" not in state and avg_net <= 0:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        family = str(row.get("family") or row.get("family_id") or "scalper_edge")
        out.append(AlphaCandidate(
            candidate_id=f"l2_loop|{exchange}|{symbol}|{family}|{state}",
            source="l2_research_loop",
            family=family,
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("horizon_ms", "?")) + "ms",
            state=state,
            route_decision=route,
            metrics={
                "samples": _num(row.get("samples")),
                "avg_net_bps": avg_net,
                "profit_factor": _num(row.get("profit_factor") or row.get("maker_profit_factor")),
            },
            evidence=row,
        ))
    alpha = (payload or {}).get("alpha_factory") or {}
    for row in alpha.get("hypotheses", []):
        if not isinstance(row, dict):
            continue
        route = _route_label(row.get("route_decision"))
        state = str(row.get("state", "UNKNOWN"))
        avg_net = _num(row.get("avg_net_bps") or row.get("maker_avg_net_bps"))
        if "EDGE_CANDIDATE" not in state and avg_net <= 0:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        family = str(row.get("family") or row.get("family_id") or "alpha_factory")
        out.append(AlphaCandidate(
            candidate_id=f"alpha_factory|{exchange}|{symbol}|{family}|{state}",
            source="alpha_factory",
            family=family,
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("horizon_ms", "?")) + "ms",
            state=state,
            route_decision=route,
            metrics={
                "samples": _num(row.get("samples")),
                "avg_net_bps": avg_net,
                "profit_factor": _num(row.get("profit_factor") or row.get("maker_profit_factor")),
            },
            evidence=row,
        ))
    return out


def _daily_scalper_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    for row in (payload or {}).get("candidates", []):
        if not isinstance(row, dict):
            continue
        score = _num(row.get("score"))
        if score <= 0:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        family = str(row.get("family") or row.get("strategy", "daily_scalper_pack"))
        out.append(AlphaCandidate(
            candidate_id=f"daily_scalper|{exchange}|{symbol}|{family}",
            source="daily_scalper_pack",
            family=family,
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("timeframe", "mixed")),
            state=str(row.get("state", "CANDIDATE")),
            route_decision=_route_label(row.get("route_decision")),
            metrics={"score": score},
            evidence=row,
        ))
    return out


def _alpha_distillation_candidates(payload: dict | None) -> list[AlphaCandidate]:
    out: list[AlphaCandidate] = []
    for row in (payload or {}).get("distilled_candidates", []):
        if not isinstance(row, dict):
            continue
        score = _num(row.get("score"))
        if score <= 0:
            continue
        exchange = str(row.get("exchange", "unknown"))
        symbol = str(row.get("symbol", "unknown"))
        family = str(row.get("family") or row.get("strategy", "alpha_distillation"))
        out.append(AlphaCandidate(
            candidate_id=f"alpha_distillation|{exchange}|{symbol}|{family}",
            source="alpha_distillation",
            family=family,
            exchange=exchange,
            symbol=symbol,
            timeframe=str(row.get("timeframe", "mixed")),
            state=str(row.get("state", "CANDIDATE")),
            route_decision=_route_label(row.get("route_decision")),
            metrics={"score": score},
            evidence=row,
        ))
    return out


def _edge_advocate(candidate: AlphaCandidate) -> AgentOpinion:
    m = candidate.metrics
    delta = 0.0
    notes: list[str] = []
    samples = _sample_count(candidate)
    if candidate.state == "PASS":
        delta += 28
        notes.append("rolling OOS gate currently marks this lane PASS")
    if "EDGE_CANDIDATE" in candidate.state:
        delta += 30
        notes.append("research miner labels this as an edge candidate")
    if samples >= 20 and _best_profit_factor(m) >= 1.5:
        delta += 12
        notes.append(f"profit factor is strong at {_best_profit_factor(m):.2f}")
    elif 0 < samples < 20 and _best_profit_factor(m) >= 1.5:
        delta += 2
        notes.append(
            f"PF={_best_profit_factor(m):.2f} is only a scout pulse with "
            f"{samples:.0f} sample(s)"
        )
    if samples >= 20 and _best_net_bps(m) > 5:
        delta += 10
        notes.append(f"net edge clears a useful bps buffer: {_best_net_bps(m):.2f}")
    elif 0 < samples < 20 and _best_net_bps(m) > 5:
        delta += 2
        notes.append(
            f"net bps pulse is positive but under-sampled: {_best_net_bps(m):.2f}"
        )
    if _num(m.get("oos_net_usd")) > 0:
        delta += 8
        notes.append(f"OOS net is positive: ${_num(m.get('oos_net_usd')):.2f}")
    if not notes:
        delta -= 8
        notes.append("no strong positive edge evidence survived extraction")
    return AgentOpinion("edge_advocate", "support", delta, "; ".join(notes))


def _skeptic(candidate: AlphaCandidate) -> AgentOpinion:
    m = candidate.metrics
    delta = 0.0
    notes: list[str] = []
    vetoes: list[str] = []
    samples = max(_num(m.get("samples")), _num(m.get("oos_trades")))
    if candidate.state == "REJECT":
        delta -= 25
        notes.append("rolling research verdict is REJECT")
    if "UNDER_SAMPLED" in candidate.state or 0 < samples < 20:
        delta -= 24
        vetoes.append("needs_more_samples")
        notes.append(f"sample count is not robust yet: {samples:.0f}")
    if samples == 0:
        delta -= 18
        vetoes.append("no_trade_sample")
        notes.append("no filled/replayed sample is available")
    if _best_profit_factor(m) and _best_profit_factor(m) < 1.25:
        delta -= 14
        notes.append(f"profit factor is below offensive threshold: {_best_profit_factor(m):.2f}")
    reasons = candidate.evidence.get("reasons") or candidate.evidence.get("why_no_trade") or []
    if reasons:
        delta -= min(12, 2 * len(reasons))
        notes.append("existing gate reasons remain unresolved")
    if not notes:
        delta += 4
        notes.append("no obvious overfit/sample-size objection in current artifact")
    return AgentOpinion("skeptic", "challenge", delta, "; ".join(notes), tuple(vetoes))


def _execution_specialist(candidate: AlphaCandidate) -> AgentOpinion:
    route = candidate.route_decision
    m = candidate.metrics
    delta = 0.0
    notes: list[str] = []
    vetoes: list[str] = []
    if route == "BLOCKED":
        delta -= 28
        vetoes.append("execution_route_blocked")
        notes.append("route decision is BLOCKED")
    elif route == "TAKER_ALLOWED":
        delta += 16
        notes.append("taker route is allowed by current evidence")
    elif route == "MAKER_ONLY":
        delta += 8
        vetoes.append("maker_fill_unproven")
        notes.append("maker-only route needs queue/fill proof")
    elif route == "CANDLE_RESEARCH":
        delta -= 4
        vetoes.append("execution_not_modeled_at_tick_level")
        notes.append("candle research does not prove intrabar execution")
    net_bps = _best_net_bps(m)
    if net_bps > 8:
        delta += 10
        notes.append(f"net bps appears above scalper fee wall: {net_bps:.2f}")
    elif candidate.source in {"fast_l2_scout", "l2_research_loop", "alpha_factory"}:
        delta -= 10
        notes.append(f"net bps buffer is weak for scalping: {net_bps:.2f}")
    return AgentOpinion(
        "execution_specialist",
        "route_check",
        delta,
        "; ".join(notes) or "no route-specific evidence",
        tuple(vetoes),
    )


def _risk_governor(candidate: AlphaCandidate) -> AgentOpinion:
    vetoes = ["research_only_no_auto_trade", "requires_human_promotion"]
    notes = ["council output is advisory only"]
    delta = -10.0
    if candidate.source in {"event_leadlag_alpha", "fast_l2_scout", "l2_research_loop", "alpha_factory"}:
        vetoes.append("requires_conservative_l2_replay")
        notes.append("microstructure candidate requires conservative replay")
    if candidate.source == "rolling_walk_forward":
        vetoes.append("requires_untouched_judgment")
        notes.append("rolling PASS still needs pre-registered untouched judgment")
    return AgentOpinion("risk_governor", "veto", delta, "; ".join(notes), tuple(vetoes))


def _research_director(candidate: AlphaCandidate) -> AgentOpinion:
    action = _next_action(candidate, (), 50.0)
    delta = 6.0 if action not in {"IGNORE", "RESEARCH_MORE"} else -2.0
    return AgentOpinion(
        "research_director",
        "next_experiment",
        delta,
        f"recommended next proof step: {action}",
    )


def _next_action(candidate: AlphaCandidate, vetoes: Iterable[str], priority: float) -> str:
    veto_set = set(vetoes)
    if candidate.source in {"event_leadlag_alpha", "fast_l2_scout", "l2_research_loop", "alpha_factory"}:
        if "needs_more_samples" in veto_set:
            return "RECORD_MORE_TICKS"
        if "execution_route_blocked" in veto_set and priority < 55:
            return "RESEARCH_MORE"
        if "maker_fill_unproven" in veto_set or "requires_conservative_l2_replay" in veto_set:
            return "RUN_CONSERVATIVE_L2_REPLAY"
        return "QUEUE_SHADOW_TRIAL_AFTER_REPLAY"
    if "execution_route_blocked" in veto_set and priority < 55:
        return "RESEARCH_MORE"
    if candidate.state == "PASS":
        return "PRE_REGISTER_UNTOUCHED_JUDGMENT"
    if _num(candidate.metrics.get("oos_net_usd")) > 0:
        return "DIAGNOSE_CLOSE_REJECT"
    return "RESEARCH_MORE"


def _verdict(priority: float, vetoes: Iterable[str]) -> str:
    veto_set = set(vetoes)
    if "execution_route_blocked" in veto_set and priority < 55:
        return "BLOCKED"
    if priority >= 75:
        return "HIGH_PRIORITY_RESEARCH"
    if priority >= 55:
        return "WATCHLIST"
    return "LOW_PRIORITY"


def publish_alpha_council(payload: dict, out: Path, feed: Path | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def _summary(debates: list[dict], candidates: list[AlphaCandidate]) -> dict:
    actions: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    sources: dict[str, int] = {}
    for row in debates:
        actions[row["next_action"]] = actions.get(row["next_action"], 0) + 1
        verdicts[row["council_verdict"]] = verdicts.get(row["council_verdict"], 0) + 1
        source = row["candidate"]["source"]
        sources[source] = sources.get(source, 0) + 1
    return {
        "candidates": len(candidates),
        "debated": len(debates),
        "high_priority": verdicts.get("HIGH_PRIORITY_RESEARCH", 0),
        "watchlist": verdicts.get("WATCHLIST", 0),
        "blocked": verdicts.get("BLOCKED", 0),
        "actions": actions,
        "sources": sources,
        "top_candidate": debates[0]["candidate"]["candidate_id"] if debates else None,
    }


def _dedupe_candidates(candidates: list[AlphaCandidate]) -> list[AlphaCandidate]:
    seen: set[str] = set()
    unique: list[AlphaCandidate] = []
    for candidate in sorted(candidates, key=_candidate_sort_key):
        if candidate.candidate_id in seen:
            continue
        seen.add(candidate.candidate_id)
        unique.append(candidate)
    return unique


def _candidate_sort_key(candidate: AlphaCandidate) -> tuple[float, str]:
    m = candidate.metrics
    samples = _sample_count(candidate)
    state_score = 0.0
    if "EDGE_CANDIDATE_TAKER" in candidate.state:
        state_score = 120.0
    elif "EDGE_CANDIDATE_MAKER" in candidate.state:
        state_score = 110.0
    elif candidate.state == "PASS":
        state_score = 100.0
    elif "UNDER_SAMPLED" in candidate.state:
        state_score = 20.0
    elif candidate.state == "REJECT":
        state_score = -20.0
    score = (
        state_score
        + min(_best_profit_factor(m), 5.0)
        + _best_net_bps(m) / 10
        + _num(m.get("oos_net_usd")) / 100
        + min(samples, 100.0) / 20
    )
    return (-score, candidate.candidate_id)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _route_label(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("route") or value.get("decision") or value.get("route_decision") or "UNKNOWN")
    if value is None:
        return "UNKNOWN"
    return str(value)


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _best_profit_factor(metrics: dict[str, Any]) -> float:
    return max(
        _num(metrics.get("profit_factor")),
        _num(metrics.get("maker_profit_factor")),
        _num(metrics.get("taker_profit_factor")),
    )


def _best_net_bps(metrics: dict[str, Any]) -> float:
    return max(
        _num(metrics.get("avg_net_bps")),
        _num(metrics.get("maker_avg_net_bps")),
        _num(metrics.get("taker_avg_net_bps")),
    )


def _sample_count(candidate: AlphaCandidate) -> float:
    return max(
        _num(candidate.metrics.get("samples")),
        _num(candidate.metrics.get("oos_trades")),
        _num(candidate.metrics.get("trades")),
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the VNEDGE Alpha Council")
    parser.add_argument("--research-dir", default=str(DEFAULT_RESEARCH_DIR))
    parser.add_argument("--out", default=str(DEFAULT_LATEST))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--max-candidates", type=int, default=30)
    parser.add_argument("--interval-seconds", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = run_alpha_council(
            args.research_dir,
            max_candidates=args.max_candidates,
        )
        publish_alpha_council(payload, Path(args.out), Path(args.feed) if args.feed else None)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            summary = payload["summary"]
            print(
                f"{payload['generated_at']} {COUNCIL_ID}: "
                f"{summary['debated']} debated, top={summary['top_candidate']}"
            )
        if args.once:
            return 0
        time.sleep(max(args.interval_seconds, 1))


if __name__ == "__main__":
    raise SystemExit(main())
