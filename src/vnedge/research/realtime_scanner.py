"""Real-time scanner: current live lane pressure, not replay proof.

Replay answers "did this edge survive historical execution assumptions?"
This scanner answers "what is forming right now in shadow/paper journals?"

It is intentionally read-only:
- consumes live ``lane_eval`` / ``shadow_intent`` / ``shadow_outcome`` journal
  tails and the event lead-lag shadow latest artifact;
- never reads candidate replay artifacts;
- emits ``can_trade=false`` and ``can_promote=false`` on every payload.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_RESEARCH_DIR = Path("research/live_research")
DEFAULT_JOURNAL_DIR = Path("logs/paper_trials")
DEFAULT_OUT = DEFAULT_RESEARCH_DIR / "realtime_scanner_latest.json"
DEFAULT_FEED = DEFAULT_RESEARCH_DIR / "realtime_scanner_feed.jsonl"
EVENT_LEADLAG_LATEST = "event_leadlag_shadow_latest.json"

STATE_FIRING = "FIRING"
STATE_NEAR_TRIGGER = "NEAR_TRIGGER"
STATE_WAITING = "WAITING"
STATE_WARMING = "WARMING"
STATE_STALE = "STALE"
STATE_NO_EVAL = "NO_EVAL"


@dataclass(frozen=True)
class RealtimeScannerConfig:
    max_eval_age_seconds: float = 3 * 3600
    near_trigger_ratio: float = 0.90
    tail_bytes: int = 1_000_000
    max_rows: int = 100

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_realtime_scanner(
    *,
    research_dir: Path | str = DEFAULT_RESEARCH_DIR,
    journal_dir: Path | str = DEFAULT_JOURNAL_DIR,
    config: RealtimeScannerConfig = RealtimeScannerConfig(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build an operator-facing current scanner report.

    The report is generated from live runtime journals only. It deliberately
    does not inspect replay/candidate-replay outputs, so operators can keep the
    mental model clean: this is current pressure, not proof or promotion.
    """
    now = now or datetime.now(UTC)
    research_dir = Path(research_dir)
    journal_dir = Path(journal_dir)

    rows = _runtime_rows(journal_dir, config=config, now=now)
    rows.extend(_event_rows(research_dir / EVENT_LEADLAG_LATEST, config=config, now=now))
    rows.sort(key=_row_sort_key)
    rows = rows[: max(1, int(config.max_rows))]

    summary = _summary(rows)
    return {
        "generated_at": now.isoformat(),
        "scanner_id": "realtime_scanner_v1",
        "mode": "live_observation_not_replay",
        "policy": {
            "status": "read_only_realtime_scanner",
            "uses_replay": False,
            "uses_live_journals": True,
            "can_trade": False,
            "can_promote": False,
            "promotion_still_requires_replay_shadow_and_human_gate": True,
        },
        "config": config.to_dict(),
        "inputs": {
            "journal_dir": str(journal_dir),
            "event_leadlag_latest": str(research_dir / EVENT_LEADLAG_LATEST),
        },
        "summary": summary,
        "rows": rows,
        "operator_answer": _operator_answer(summary),
        "can_trade": False,
        "can_promote": False,
    }


def publish_realtime_scanner(
    payload: Mapping[str, Any], out: Path, feed: Path | None = None
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with open(feed, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def render_report(payload: Mapping[str, Any], *, limit: int = 20) -> str:
    summary = payload.get("summary", {})
    lines = [
        "=== Real-time scanner ===",
        f"generated: {payload.get('generated_at')}",
        str(payload.get("operator_answer") or ""),
        (
            "summary: "
            f"{summary.get('total_rows', 0)} rows, "
            f"{summary.get('paper_lanes', 0)} paper, "
            f"{summary.get('firing', 0)} firing, "
            f"{summary.get('near_trigger', 0)} near, "
            f"{summary.get('waiting', 0)} waiting, "
            f"{summary.get('stale', 0)} stale"
        ),
    ]
    for row in list(payload.get("rows", []))[:limit]:
        lines.append(
            f"  {row.get('state', STATE_WAITING):<14} "
            f"{row.get('exchange', ''):<14} {row.get('symbol', ''):<14} "
            f"{row.get('strategy_id') or row.get('runner_id') or row.get('row_type', ''):<26} "
            f"{row.get('why', '')}"
        )
    lines.append("read-only: live scanner, not replay; can_trade=false can_promote=false")
    return "\n".join(lines)


def _runtime_rows(
    journal_dir: Path,
    *,
    config: RealtimeScannerConfig,
    now: datetime,
) -> list[dict[str, Any]]:
    if not journal_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    # Runtime lane journals live here. Include paper and shadow, but only rows
    # with lane_eval become scanner lanes; other journals are ignored.
    for path in sorted(journal_dir.glob("*.journal.jsonl")):
        row = _runtime_row_from_journal(path, config=config, now=now)
        if row is not None:
            rows.append(row)
    return rows


def _runtime_row_from_journal(
    path: Path,
    *,
    config: RealtimeScannerConfig,
    now: datetime,
) -> dict[str, Any] | None:
    latest_eval: dict[str, Any] | None = None
    latest_eval_ts: datetime | None = None
    first_record_ts: datetime | None = None
    funnel = {
        "evals": 0,
        "live_evals": 0,
        "backfill_evals": 0,
        "signals": 0,
        "live_signals": 0,
        "backfill_signals": 0,
        "shadow_intents": 0,
        "approved_shadow_intents": 0,
        "rejected_shadow_intents": 0,
        "shadow_outcomes": 0,
        "risk_decisions": 0,
        "paper_order_intents": 0,
        "paper_order_acknowledged": 0,
        "paper_exits": 0,
        "paper_reports": 0,
    }
    latest_intent: dict[str, Any] | None = None
    latest_outcome: dict[str, Any] | None = None
    latest_paper_order: dict[str, Any] | None = None
    latest_paper_exit: dict[str, Any] | None = None
    latest_paper_report: dict[str, Any] | None = None

    for record in _iter_jsonl(path, max_bytes=config.tail_bytes):
        record_ts = _parse_dt(record.get("ts"))
        if record_ts is not None and first_record_ts is None:
            first_record_ts = record_ts
        kind = str(record.get("kind") or "")
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if kind == "lane_eval":
            funnel["evals"] += 1
            backfill = bool(payload.get("backfill"))
            fired = bool(payload.get("fired"))
            if backfill:
                funnel["backfill_evals"] += 1
                if fired:
                    funnel["backfill_signals"] += 1
            else:
                funnel["live_evals"] += 1
                if fired:
                    funnel["live_signals"] += 1
            if fired:
                funnel["signals"] += 1
            if not backfill:
                latest_eval = dict(payload)
                latest_eval_ts = record_ts or _parse_dt(payload.get("bar_ts"))
        elif kind == "shadow_intent":
            funnel["shadow_intents"] += 1
            if payload.get("approved"):
                funnel["approved_shadow_intents"] += 1
            else:
                funnel["rejected_shadow_intents"] += 1
            latest_intent = dict(payload)
        elif kind == "shadow_outcome":
            funnel["shadow_outcomes"] += 1
            latest_outcome = dict(payload)
        elif kind == "risk_decision":
            funnel["risk_decisions"] += 1
        elif kind == "order_intent":
            funnel["paper_order_intents"] += 1
            latest_paper_order = dict(payload)
        elif kind == "order_acknowledged":
            funnel["paper_order_acknowledged"] += 1
        elif kind == "live_paper_exit":
            funnel["paper_exits"] += 1
            latest_paper_exit = dict(payload)
        elif kind == "live_paper_report":
            funnel["paper_reports"] += 1
            latest_paper_report = dict(payload.get("report") or payload)

    if latest_eval is None:
        return None

    age_seconds = _age_seconds(latest_eval_ts, now)
    state, why, proximity = _runtime_state(latest_eval, age_seconds, config)
    intent = latest_intent.get("intent") if isinstance(latest_intent, Mapping) else {}
    exchange = str(
        latest_eval.get("exchange")
        or (intent.get("exchange") if isinstance(intent, Mapping) else "")
        or _exchange_hint(path.name)
    )
    symbol = str(
        latest_eval.get("symbol")
        or (intent.get("symbol") if isinstance(intent, Mapping) else "")
        or ""
    )
    strategy_id = str(
        latest_eval.get("strategy_id")
        or (intent.get("strategy_id") if isinstance(intent, Mapping) else "")
        or path.name.removesuffix(".journal.jsonl")
    )
    live_fire_rate = (
        funnel["live_signals"] / funnel["live_evals"]
        if funnel["live_evals"]
        else None
    )
    return {
        "row_type": "runtime_lane",
        "lane_id": path.name.removesuffix(".journal.jsonl"),
        "journal": str(path),
        "exchange": exchange,
        "symbol": symbol,
        "strategy_id": strategy_id,
        "mode": str(latest_eval.get("mode") or ""),
        "state": state,
        "why": why,
        "latest_eval_ts": latest_eval_ts.isoformat() if latest_eval_ts else None,
        "latest_bar_ts": latest_eval.get("bar_ts"),
        "age_seconds": age_seconds,
        "active_since_ts": first_record_ts.isoformat() if first_record_ts else None,
        "funnel": funnel,
        "live_fire_rate": live_fire_rate,
        "latest_eval": {
            "fired": bool(latest_eval.get("fired")),
            "signal_reason": latest_eval.get("signal_reason"),
            "skip_reason": latest_eval.get("skip_reason"),
            "features": latest_eval.get("features") or {},
            "thresholds": latest_eval.get("thresholds") or {},
        },
        "latest_shadow_intent": _compact_intent(latest_intent),
        "latest_shadow_outcome": _compact_outcome(latest_outcome),
        "latest_paper_order": _compact_paper_order(latest_paper_order),
        "latest_paper_exit": latest_paper_exit,
        "latest_paper_report": latest_paper_report,
        "proximity": proximity,
        "can_trade": False,
        "can_promote": False,
        "requires_replay_for_promotion": True,
    }


def _runtime_state(
    latest_eval: Mapping[str, Any],
    age_seconds: float | None,
    config: RealtimeScannerConfig,
) -> tuple[str, str, list[dict[str, Any]]]:
    if age_seconds is not None and age_seconds > config.max_eval_age_seconds:
        return STATE_STALE, f"last live eval {int(age_seconds)}s old", []
    if bool(latest_eval.get("fired")):
        return (
            STATE_FIRING,
            str(latest_eval.get("signal_reason") or "signal fired"),
            _threshold_pairs(latest_eval),
        )
    if latest_eval.get("skip_reason"):
        return (
            STATE_WAITING,
            f"entry blocked: {latest_eval.get('skip_reason')}",
            _threshold_pairs(latest_eval),
        )

    features = latest_eval.get("features") or {}
    pairs = _threshold_pairs(latest_eval)
    if isinstance(features, Mapping) and any(v is None for v in features.values()):
        if not pairs:
            return STATE_WARMING, "feature warmup incomplete", []
    if not pairs:
        return STATE_WAITING, "no threshold telemetry exposed", []
    unmet = [item for item in pairs if float(item.get("gap") or 0.0) > 0.0]
    best = max(unmet or pairs, key=lambda item: float(item.get("ratio") or 0.0))
    ratio = float(best.get("ratio") or 0.0)
    if ratio >= config.near_trigger_ratio:
        return (
            STATE_NEAR_TRIGGER,
            (
                f"{best['name']} {best['value']:.4g}/{best['threshold']:.4g} "
                f"({ratio * 100:.0f}% of trigger)"
            ),
            pairs,
        )
    return (
        STATE_WAITING,
        (
            f"{best['name']} {best['value']:.4g}/{best['threshold']:.4g}; "
            f"below trigger"
        ),
        pairs,
    )


def _threshold_pairs(eval_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    features = eval_payload.get("features") or {}
    thresholds = eval_payload.get("thresholds") or {}
    if not isinstance(features, Mapping) or not isinstance(thresholds, Mapping):
        return []
    pairs: list[dict[str, Any]] = []
    _add_min_pair(
        pairs,
        "funding",
        _num(features.get("funding_pct")),
        _num(thresholds.get("extreme_pct")),
        absolute=True,
    )
    _add_min_pair(
        pairs,
        "z",
        _num(features.get("close_z")),
        _num(thresholds.get("z_entry")),
        absolute=True,
    )
    _add_min_pair(
        pairs,
        "score",
        _first_num(
            features.get("score"),
            _max_num(features.get("long_score"), features.get("short_score")),
        ),
        _num(thresholds.get("min_score")),
        absolute=False,
    )
    _add_min_pair(
        pairs,
        "score_delta",
        _score_delta(features),
        _num(thresholds.get("min_score_delta")),
        absolute=False,
    )
    _add_min_pair(
        pairs,
        "tqi",
        _max_num(features.get("tqi_long"), features.get("tqi_short")),
        _num(thresholds.get("min_tqi")),
        absolute=False,
    )
    _add_min_pair(
        pairs,
        "quality_strength",
        _num(features.get("quality_strength")),
        _num(thresholds.get("min_quality_strength")),
        absolute=False,
    )
    _add_min_pair(
        pairs,
        "momentum_persistence",
        _max_num(features.get("mom_persist_long"), features.get("mom_persist_short")),
        _num(thresholds.get("min_momentum_persistence")),
        absolute=False,
    )
    _add_min_pair(
        pairs,
        "bbp_atr",
        _num(features.get("bbp")),
        _num(thresholds.get("min_bbp_atr")),
        absolute=True,
    )
    _add_min_pair(
        pairs,
        "bbp_z",
        _num(features.get("bbp_hist_z")),
        _num(thresholds.get("min_bbp_z")),
        absolute=True,
    )
    _add_min_pair(
        pairs,
        "volume_z",
        _num(features.get("volume_z")),
        _num(thresholds.get("min_volume_z")),
        absolute=False,
    )
    _add_min_pair(
        pairs,
        "body_atr",
        _num(features.get("body_atr")),
        _num(thresholds.get("min_body_atr")),
        absolute=False,
    )
    _add_min_pair(
        pairs,
        "body_percentile",
        _num(features.get("body_percentile")),
        _num(thresholds.get("min_body_percentile")),
        absolute=False,
    )
    _add_min_pair(
        pairs,
        "expected_net_edge_bps",
        _max_num(
            features.get("expected_net_edge_bps_long"),
            features.get("expected_net_edge_bps_short"),
        ),
        _num(thresholds.get("min_expected_net_edge_bps")),
        absolute=False,
    )
    return pairs


def _add_min_pair(
    out: list[dict[str, Any]],
    name: str,
    value: float | None,
    threshold: float | None,
    *,
    absolute: bool,
) -> None:
    if value is None or threshold is None:
        return
    observed = abs(value) if absolute else value
    if threshold > 0:
        ratio = observed / threshold
    else:
        gap = max(0.0, threshold - observed)
        ratio = 1.0 - gap / max(1.0, abs(threshold))
    out.append({
        "name": name,
        "value": observed,
        "raw_value": value,
        "threshold": threshold,
        "ratio": max(0.0, ratio),
        "gap": max(0.0, threshold - observed),
    })


def _first_num(*values: Any) -> float | None:
    for value in values:
        out = _num(value)
        if out is not None:
            return out
    return None


def _max_num(*values: Any) -> float | None:
    nums = [_num(value) for value in values]
    present = [value for value in nums if value is not None]
    return max(present) if present else None


def _score_delta(features: Mapping[str, Any]) -> float | None:
    long_score = _num(features.get("long_score"))
    short_score = _num(features.get("short_score"))
    if long_score is None or short_score is None:
        return None
    return abs(long_score - short_score)


def _event_rows(
    path: Path,
    *,
    config: RealtimeScannerConfig,
    now: datetime,
) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if not payload:
        return []
    evaluations = payload.get("evaluations")
    if not isinstance(evaluations, list):
        return []
    rows: list[dict[str, Any]] = []
    for row in evaluations:
        if not isinstance(row, Mapping):
            continue
        generated_at = _parse_dt(row.get("generated_at") or payload.get("generated_at"))
        age_seconds = _age_seconds(generated_at, now)
        state = STATE_FIRING if row.get("fired") or row.get("shadow_intent") else STATE_WAITING
        if age_seconds is not None and age_seconds > config.max_eval_age_seconds:
            state = STATE_STALE
        why = str(row.get("signal_reason") or "")
        intent = row.get("shadow_intent")
        if not why and isinstance(intent, Mapping):
            why = str(intent.get("signal_reason") or "")
        if not why:
            reasons = _list(row.get("why_no_trade"))
            why = "; ".join(reasons[:3]) if reasons else str(row.get("state") or "event filter not met")
        spec_id = str(row.get("spec_id") or "event_leadlag_shadow")
        rows.append({
            "row_type": "event_leadlag_shadow",
            "lane_id": spec_id,
            "runner_id": str(row.get("runner_id") or "event_leadlag_shadow_runner"),
            "exchange": str(row.get("follower_exchange") or ""),
            "symbol": str(row.get("follower_symbol") or ""),
            "leader": {
                "exchange": row.get("leader_exchange"),
                "symbol": row.get("leader_symbol"),
            },
            "strategy_id": "event_leadlag_shadow",
            "mode": "shadow_observation",
            "state": state,
            "why": why,
            "latest_eval_ts": generated_at.isoformat() if generated_at else None,
            "age_seconds": age_seconds,
            "funnel": {
                "evals": 1,
                "live_evals": 1,
                "signals": 1 if state == STATE_FIRING else 0,
                "shadow_intents": 1 if row.get("shadow_intent") else 0,
            },
            "latest_eval": {
                "fired": bool(row.get("fired") or row.get("shadow_intent")),
                "signal_reason": row.get("signal_reason"),
                "skip_reason": why if state != STATE_FIRING else None,
                "features": row.get("metrics") or {},
                "thresholds": (row.get("metrics") or {}).get("filter", {}),
            },
            "proximity": _event_proximity(row),
            "can_trade": False,
            "can_promote": False,
            "requires_replay_for_promotion": True,
        })
    return rows


def _event_proximity(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    metrics = row.get("metrics") or {}
    if not isinstance(metrics, Mapping):
        return []
    filt = metrics.get("filter") or {}
    if not isinstance(filt, Mapping):
        return []
    out: list[dict[str, Any]] = []
    _add_min_pair(
        out,
        "leader_bps",
        _num(metrics.get("signed_leader_bps")),
        _num(filt.get("min_abs_leader_bps")),
        absolute=True,
    )
    _add_min_pair(
        out,
        "leader_z",
        _num(metrics.get("signed_leader_z")),
        _num(filt.get("min_abs_leader_z")),
        absolute=True,
    )
    _add_min_pair(
        out,
        "volume_z",
        _num(metrics.get("leader_volume_z")),
        _num(filt.get("min_volume_z")),
        absolute=False,
    )
    return out


def _summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    counts: dict[str, int] = {
        STATE_FIRING: 0,
        STATE_NEAR_TRIGGER: 0,
        STATE_WAITING: 0,
        STATE_WARMING: 0,
        STATE_STALE: 0,
        STATE_NO_EVAL: 0,
    }
    for row in rows:
        state = str(row.get("state") or STATE_WAITING)
        counts[state] = counts.get(state, 0) + 1
    return {
        "total_rows": len(rows),
        "runtime_lanes": sum(1 for row in rows if row.get("row_type") == "runtime_lane"),
        "paper_lanes": sum(
            1
            for row in rows
            if row.get("row_type") == "runtime_lane" and row.get("mode") == "paper"
        ),
        "paper_firing": sum(
            1
            for row in rows
            if row.get("row_type") == "runtime_lane"
            and row.get("mode") == "paper"
            and row.get("state") == STATE_FIRING
        ),
        "paper_order_intents": sum(
            int(row.get("funnel", {}).get("paper_order_intents") or 0)
            for row in rows
            if row.get("row_type") == "runtime_lane" and row.get("mode") == "paper"
        ),
        "paper_exits": sum(
            int(row.get("funnel", {}).get("paper_exits") or 0)
            for row in rows
            if row.get("row_type") == "runtime_lane" and row.get("mode") == "paper"
        ),
        "event_lanes": sum(1 for row in rows if row.get("row_type") == "event_leadlag_shadow"),
        "firing": counts.get(STATE_FIRING, 0),
        "near_trigger": counts.get(STATE_NEAR_TRIGGER, 0),
        "waiting": counts.get(STATE_WAITING, 0),
        "warming": counts.get(STATE_WARMING, 0),
        "stale": counts.get(STATE_STALE, 0),
        "scanner_not_replay": True,
        "can_trade": False,
        "can_promote": False,
    }


def _operator_answer(summary: Mapping[str, Any]) -> str:
    total = int(summary.get("total_rows") or 0)
    firing = int(summary.get("firing") or 0)
    near = int(summary.get("near_trigger") or 0)
    stale = int(summary.get("stale") or 0)
    paper_lanes = int(summary.get("paper_lanes") or 0)
    paper_firing = int(summary.get("paper_firing") or 0)
    paper_orders = int(summary.get("paper_order_intents") or 0)
    paper_exits = int(summary.get("paper_exits") or 0)
    if total == 0:
        return (
            "No live scanner rows yet. This report only reads runtime journals; "
            "replay artifacts may exist but are not used here."
        )
    if firing:
        return (
            f"{firing} lane(s) are firing now, including {paper_firing} paper lane(s). "
            f"Paper activity: {paper_lanes} lane(s), {paper_orders} order intents, "
            f"{paper_exits} exits. This is shadow/paper observation, "
            "not permission to promote or trade."
        )
    if near:
        return (
            f"No current fire; {near} lane(s) are near trigger. Watch these first, "
            "then require replay/shadow proof before promotion."
        )
    if stale:
        return (
            f"No current fire; {stale} lane(s) have stale live evaluations. "
            "Fix data/runtime freshness before tuning thresholds."
        )
    return (
        "No current fire. Lanes are evaluating live data, but features are below "
        "their configured thresholds."
    )


def _row_sort_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
    state_rank = {
        STATE_FIRING: 0,
        STATE_NEAR_TRIGGER: 1,
        STATE_WAITING: 2,
        STATE_WARMING: 3,
        STATE_STALE: 4,
        STATE_NO_EVAL: 5,
    }
    proximity = row.get("proximity")
    best_ratio = 0.0
    if isinstance(proximity, list) and proximity:
        ratios = [
            float(item.get("ratio") or 0.0)
            for item in proximity
            if isinstance(item, Mapping)
        ]
        best_ratio = max(ratios) if ratios else 0.0
    return (
        state_rank.get(str(row.get("state")), 9),
        -best_ratio,
        str(row.get("lane_id") or ""),
    )


def _compact_intent(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    intent = payload.get("intent") or {}
    return {
        "intent_key": payload.get("intent_key"),
        "approved": bool(payload.get("approved")),
        "failed_checks": payload.get("failed_checks") or [],
        "side": intent.get("side") if isinstance(intent, Mapping) else None,
        "notional_usd": intent.get("notional_usd") if isinstance(intent, Mapping) else None,
        "signal_reason": payload.get("signal_reason"),
    }


def _compact_paper_order(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    intent = payload.get("intent") or {}
    if not isinstance(intent, Mapping):
        return {"intent_key": payload.get("intent_key")}
    return {
        "intent_key": payload.get("intent_key"),
        "client_order_id": payload.get("client_order_id"),
        "side": intent.get("side"),
        "quantity": intent.get("quantity"),
        "reduce_only": intent.get("reduce_only"),
        "strategy_id": intent.get("strategy_id"),
        "symbol": intent.get("symbol"),
    }


def _compact_outcome(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    return {
        "intent_key": payload.get("intent_key"),
        "net_usd": payload.get("net_usd", payload.get("virtual_net_usd")),
        "exit_reason": payload.get("exit_reason", payload.get("resolution")),
        "bars_held": payload.get("bars_held"),
    }


def _exchange_hint(name: str) -> str:
    for exchange in ("delta_india", "binanceusdm", "binance", "bybit"):
        if exchange in name:
            return exchange
    return ""


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return [line for line in lines if line.strip()]


def _iter_jsonl(path: Path, *, max_bytes: int) -> Iterable[dict[str, Any]]:
    for line in _tail_lines(path, max_bytes):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _age_seconds(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    return max(0.0, (now.astimezone(UTC) - ts.astimezone(UTC)).total_seconds())


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value:
        return [str(value)]
    return []


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish real-time lane scanner from live journals (not replay)"
    )
    parser.add_argument("--research-dir", default=str(DEFAULT_RESEARCH_DIR))
    parser.add_argument("--journal-dir", default=str(DEFAULT_JOURNAL_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--feed", default=str(DEFAULT_FEED))
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--max-eval-age-seconds", type=float, default=3 * 3600)
    parser.add_argument("--near-trigger-ratio", type=float, default=0.90)
    parser.add_argument("--tail-bytes", type=int, default=1_000_000)
    parser.add_argument("--max-rows", type=int, default=100)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-publish", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = RealtimeScannerConfig(
        max_eval_age_seconds=args.max_eval_age_seconds,
        near_trigger_ratio=args.near_trigger_ratio,
        tail_bytes=args.tail_bytes,
        max_rows=args.max_rows,
    )
    while True:
        payload = build_realtime_scanner(
            research_dir=args.research_dir,
            journal_dir=args.journal_dir,
            config=config,
        )
        if not args.no_publish:
            publish_realtime_scanner(
                payload,
                Path(args.out),
                None if args.feed == "" else Path(args.feed),
            )
        print(json.dumps(payload, indent=2, default=str) if args.json else render_report(payload))
        if args.once:
            return 0
        time.sleep(max(1, int(args.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
