"""Evidence-grounded analyst projection, independent of execution permissions."""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vnedge.dashboard.analyst_public import PRODUCT_CAP, SYMBOL
from vnedge.dashboard.analyst_store import AnalystStore, digest, utc
from vnedge.dashboard.crypto_analyst import (
    EXCHANGES,
    TIMEFRAMES,
    CryptoAnalystService,
    _empty,
    analyse_rows,
    read_window,
)
from vnedge.dashboard.crypto_fundamentals import fundamentals_dossier
from vnedge.dashboard.market_stage import analyse_stage, empty_stage, stage_rows


def observation(record: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    if not record:
        return {"state": "unavailable", "issues": ["not_collected"], "values": {}}
    body = record["body"]
    try:
        received, stamp = utc(body["received_at"]), utc(body["venue_ts"])
        age = max((now - received).total_seconds(), (now - stamp).total_seconds())
        state = (
            "current"
            if 0 <= age <= body["expires_after_s"] and stamp <= received <= now
            else "stale"
        )
    except (KeyError, ValueError, TypeError):
        state = "unavailable"
    return {
        **body,
        "evidence_id": record["evidence_id"],
        "available_at": record["available_at"],
        "state": state,
    }


def dossier_cache_current(body: dict[str, Any], now: datetime) -> bool:
    """A cache TTL must never extend the underlying evidence expiry."""
    seconds = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    try:
        for row in [*body["frames"], *body["stages"]]:
            if row["state"] == "current":
                age = (now - utc(row["as_of"])).total_seconds()
                if not 0 <= age <= seconds[row["timeframe"]] * 1.5:
                    return False
        for row in body["fundamentals"]["fields"]:
            if row["status"] == "current":
                end, received = utc(row["period_end"]), utc(row["received_at"])
                if not end <= received <= now or max((now - end).total_seconds(), (now - received).total_seconds()) > row["max_age_seconds"]:
                    return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


class AnalystWorkspace:
    def __init__(self, candle_root: Path, evidence_path: Path) -> None:
        self.core = CryptoAnalystService(candle_root)
        self.store = AnalystStore(evidence_path)
        self._lock = threading.Lock()
        self._dossiers: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = OrderedDict()

    def _latest(self, kind: str, scope: str, now: datetime) -> dict[str, Any] | None:
        rows = self.store.read(kind, scope, now=now)
        return rows[0] if rows else None

    def universe(self, exchange: str, now: datetime) -> dict[str, Any]:
        try:
            record = self._latest("universe", exchange, now)
            if record:
                body = record["body"]
                age = (now - utc(body["generated_at"])).total_seconds()
                return {
                    **body,
                    "evidence_id": record["evidence_id"],
                    "state": "current" if 0 <= age <= body["expires_after_s"] else "stale",
                }
        except Exception:  # noqa: BLE001 - corrupted evidence must not take down technical analysis
            return {"state": "unavailable", "products": [], "issues": ["universe_evidence_invalid"]}
        return {"state": "unavailable", "products": [], "issues": ["public_universe_not_collected"]}

    def conditions(self, exchange: str, symbol: str, now: datetime) -> dict[str, Any]:
        result = {}
        for kind in ("conditions", "flow"):
            try:
                result[kind] = observation(self._latest(kind, exchange + "/" + symbol, now), now)
            except Exception:  # noqa: BLE001
                result[kind] = {
                    "state": "unavailable",
                    "issues": ["evidence_invalid"],
                    "values": {},
                }
        return result

    def snapshot(self, exchange: str, timeframe: str) -> dict[str, Any]:
        # Never mutate the frozen v1 core cache or its alignment calculation.
        report = copy.deepcopy(self.core.snapshot(exchange, timeframe))
        now = datetime.now(UTC)
        universe = self.universe(exchange, now)
        products = {r["symbol"]: r for r in universe["products"][:PRODUCT_CAP]}
        known = {r["symbol"] for r in report["markets"]}
        for symbol in sorted(products):
            if symbol not in known:
                report["markets"].append(
                    _empty(symbol, timeframe, "no_canonical_analysis_in_covered_universe")
                )
        for row in report["markets"]:
            row["product"] = products.get(row["symbol"])
            row["market_evidence"] = self.conditions(exchange, row["symbol"], now)
        report["workspace_schema"] = "crypto_analyst_workspace_v2"
        report["public_universe"] = universe
        report["universe"]["displayed"] = len(report["markets"])
        report["universe"]["scope"] = (
            "public product discovery + separate canonical technical coverage"
        )
        report["public_observed_at"] = now.isoformat()
        report["capabilities"] = {
            "conversation": "local_evidence_answers",
            "paid_provider_enabled": False,
            "public_collection": "delta_india_only",
            "multi_timeframe": list(TIMEFRAMES),
            "settled_funding": False,
            "ml_probability": False,
            "order_access": False,
        }
        try:
            record = self._latest("collector", exchange, now)
            report["collector"] = (
                {
                    **record["body"],
                    "evidence_id": record["evidence_id"],
                    "stale": (now - utc(record["body"]["generated_at"])).total_seconds() > 180,
                }
                if record
                else {"stale": True, "issues": ["worker_not_started"]}
            )
        except Exception:  # noqa: BLE001
            report["collector"] = {"stale": True, "issues": ["collector_evidence_invalid"]}
        return report

    def dossier(self, exchange: str, symbol: str) -> dict[str, Any]:
        if exchange not in EXCHANGES or not SYMBOL.fullmatch(symbol):
            raise ValueError("unsupported_analyst_scope")
        key = (exchange, symbol)
        with self._lock:
            now = datetime.now(UTC)
            cached = self._dossiers.get(key)
            if (
                cached
                and time.monotonic() - cached[0] < 10
                and dossier_cache_current(cached[1], now)
                and all(
                    obs.get("state") != "current"
                    or observation(
                        {
                            "body": obs,
                            "evidence_id": obs["evidence_id"],
                            "available_at": obs["available_at"],
                        },
                        now,
                    )["state"]
                    == "current"
                    for obs in cached[1]["market_evidence"].values()
                )
            ):
                return copy.deepcopy(cached[1])
            frames = []
            for tf in TIMEFRAMES:
                try:
                    frame = analyse_rows(
                        read_window(self.core.root, exchange, symbol, tf, now),
                        symbol,
                        exchange,
                        tf,
                        now,
                    )
                except Exception:  # noqa: BLE001
                    frame = _empty(symbol, tf, "series_read_failed")
                frames.append(frame)
            market = self.conditions(exchange, symbol, now)
            stages = self.stages(exchange, symbol, now)
            fundamentals = fundamentals_dossier(self.store, symbol, now)
            evidence = []
            evidence.extend(fundamentals["evidence"])
            for stage in stages:
                if stage.get("stage_id"):
                    evidence.append(
                        {
                            "id": stage["stage_id"],
                            "kind": "market_stage",
                            "as_of": stage["as_of"],
                            "state": stage["state"],
                            "source": "canonical_tick_lake",
                            "summary": f"{stage['timeframe']}: {stage['stage']}",
                        }
                    )
            for frame in frames:
                if frame.get("analysis_id"):
                    evidence.append(
                        {
                            "id": frame["analysis_id"],
                            "kind": "canonical_technical",
                            "timeframe": frame["timeframe"],
                            "as_of": frame["as_of"],
                            "state": frame["state"],
                            "source": frame["source"],
                            "summary": f"{frame['timeframe']}: {frame['bias']} alignment {frame['alignment']} / ±100",
                        }
                    )
            for kind, obs in market.items():
                if obs.get("evidence_id"):
                    evidence.append(
                        {
                            "id": obs["evidence_id"],
                            "kind": kind,
                            "as_of": obs.get("venue_ts"),
                            "state": obs["state"],
                            "source": obs.get("source", "unavailable"),
                            "summary": f"{kind}: {obs['state']} public sample; not execution evidence",
                        }
                    )
            directions = {
                f["bias"] for f in frames if f["state"] == "current" and f["bias"] != "mixed"
            }
            conflicts = (
                ["timeframe_direction_disagreement"] if {"bullish", "bearish"} <= directions else []
            )
            gaps = [f"{f['timeframe']}:{issue}" for f in frames for issue in f["issues"]]
            gaps += [
                kind + ":" + value["state"]
                for kind, value in market.items()
                if value["state"] != "current"
            ]
            body = {
                "schema": "analyst_dossier_v2",
                "exchange": exchange,
                "symbol": symbol,
                "generated_at": now.isoformat(),
                "frames": frames,
                "stages": stages,
                "fundamentals": fundamentals,
                "market_evidence": market,
                "evidence": evidence,
                "conflicts": conflicts,
                "gaps": gaps,
                "can_trade": False,
                "can_promote": False,
                "ml_probability": None,
                "clock_note": "Each timeframe has its own closed-bar as-of; public REST samples are later observations, never backfilled features.",
            }
            body["dossier_id"] = digest(
                [exchange, symbol, frames, market, stages, fundamentals["dossier_id"]]
            )
            self._dossiers[key] = (time.monotonic(), body)
            self._dossiers.move_to_end(key)
            while len(self._dossiers) > 32:
                self._dossiers.popitem(last=False)
            return copy.deepcopy(body)

    def stages(self, exchange: str, symbol: str, now: datetime) -> list[dict[str, Any]]:
        results = []
        for tf in ("4h", "1d"):
            try:
                results.append(
                    analyse_stage(
                        stage_rows(self.core.root, exchange, symbol, tf, now),
                        exchange,
                        symbol,
                        tf,
                        now,
                    )
                )
            except Exception:  # noqa: BLE001 - a stage failure never becomes a carried conclusion
                results.append(empty_stage(tf, "stage_series_read_failed"))
        return results

    def history(self, exchange: str, symbol: str) -> dict[str, Any]:
        if exchange not in EXCHANGES or not SYMBOL.fullmatch(symbol):
            raise ValueError("unsupported_analyst_scope")
        now = datetime.now(UTC)
        try:
            records = self.store.read("report", exchange + "/" + symbol, now=now, limit=30)
            events = self.store.read("change", exchange + "/" + symbol, now=now, limit=30)
            stages = self.store.read("stage_report", exchange + "/" + symbol, now=now, limit=30)
            return {
                "reports": records,
                "changes": events,
                "stage_reports": stages,
                "can_trade": False,
                "status": "recorded" if records or stages else "no_saved_reports",
            }
        except Exception:  # noqa: BLE001
            return {
                "reports": [],
                "changes": [],
                "status": "evidence_read_failed",
                "can_trade": False,
            }

    def answer(self, exchange: str, symbol: str, question: str) -> dict[str, Any]:
        """Deterministic evidence retrieval. No agent tools, paid model, or write access."""
        if not question.strip() or len(question) > 500:
            raise ValueError("question_length_bound")
        dossier = self.dossier(exchange, symbol)
        q = question.casefold()
        lines: list[dict[str, Any]] = []
        if any(
            word in q for word in ("buy", "sell", "order", "leverage", "size", "promote", "execute")
        ):
            lines.append(
                {
                    "text": "This analyst cannot recommend an order, size a position or grant trading permission.",
                    "citations": [],
                }
            )
        requested_market = any(
            word in q for word in ("funding", "interest", "book", "spread", "flow", "liquidity")
        )
        requested_gaps = any(word in q for word in ("missing", "gap", "why", "risk", "contradict"))
        if any(
            word in q for word in ("stage", "base", "accumulation", "distribution", "development")
        ):
            for stage in dossier["stages"]:
                refs = [stage["stage_id"]] if stage.get("stage_id") else []
                if stage["state"] != "current":
                    lines.append(
                        {
                            "text": f"{stage['timeframe']} stage: {stage['state']}; no current stage conclusion.",
                            "citations": refs,
                        }
                    )
                else:
                    lines.append(
                        {
                            "text": f"{stage['timeframe']}: {stage['stage']}, previously {stage['previous_stage'] or 'unknown'}, {stage['bars_in_state']} observed closes in state. History is bounded, not lifetime duration.",
                            "citations": refs,
                        }
                    )
                    for watch in stage["watch"]:
                        lines.append({"text": watch["condition"], "citations": refs})
                    lines.append({"text": stage["invalidation"], "citations": refs})
            lines.append(
                {
                    "text": "Accumulation and distribution are hypotheses, not participant identities or trading permission.",
                    "citations": [],
                }
            )
        elif any(
            word in q
            for word in (
                "fundamental",
                "revenue",
                "earnings",
                "emission",
                "unlock",
                "usage",
                "fees",
            )
        ):
            for field in dossier["fundamentals"]["fields"]:
                display = (
                    format(field["value"], ".8g")
                    if field["status"] == "current"
                    else field["status"]
                )
                lines.append(
                    {
                        "text": f"{field['label']}: {display}"
                        + (
                            f" {field.get('unit', '')} for period ending {field.get('period_end')}"
                            if field["status"] == "current"
                            else f" — {field['reason']}"
                        ),
                        "citations": [field["evidence_id"]] if field.get("evidence_id") else [],
                    }
                )
            lines.append({"text": dossier["fundamentals"]["note"], "citations": []})
        elif requested_market:
            for kind, obs in dossier["market_evidence"].items():
                refs = [obs["evidence_id"]] if obs.get("evidence_id") else []
                if obs["state"] != "current":
                    lines.append(
                        {
                            "text": f"{kind}: {obs['state']}; no current conclusion is supported.",
                            "citations": refs,
                        }
                    )
                elif kind == "conditions":
                    vals = obs["values"]
                    for key in ("spread_bps", "open_interest_contracts", "indicative_funding_pct"):
                        value = vals.get(key)
                        display = format(value, ".8g") if value is not None else "unavailable"
                        lines.append(
                            {
                                "text": f"{key}: {display} (public REST snapshot; rounded for display).",
                                "citations": refs,
                            }
                        )
                    lines.append(
                        {
                            "text": "Indicative funding is not settled cash. This book snapshot cannot establish lane quote-hold parity.",
                            "citations": refs,
                        }
                    )
                else:
                    lines.append(
                        {
                            "text": f"Aggressor-buy share: {obs['buy_share_pct']:.2f}% of sampled base volume across {obs['sample_trades']} prints. Incomplete sample, not total session flow or institution identity.",
                            "citations": refs,
                        }
                    )
        else:
            for frame in dossier["frames"]:
                refs = [frame["analysis_id"]] if frame.get("analysis_id") else []
                if frame["state"] != "current":
                    text = (
                        f"{frame['timeframe']}: {frame['state']}; cannot infer current direction."
                    )
                else:
                    text = f"{frame['timeframe']}: {frame['bias']}; alignment {frame['alignment']} / ±100, not a probability."
                    if "vwap" in q:
                        text += f" Exact session VWAP: {frame['metrics'].get('session_vwap')} (None means unavailable)."
                    elif any(word in q for word in ("level", "breakout", "support", "resistance")):
                        text += f" Prior range {frame['metrics']['range_low']:.8g}–{frame['metrics']['range_high']:.8g}; reference levels, not entries."
                lines.append({"text": text, "citations": refs})
        if requested_gaps or dossier["conflicts"]:
            lines.append(
                {
                    "text": "Evidence gaps / contradictions: "
                    + ", ".join(
                        dossier["gaps"] + dossier["conflicts"]
                        or ["none reported; no edge validation implied"]
                    ),
                    "citations": [r["id"] for r in dossier["evidence"]],
                }
            )
        body = {
            "mode": "local_evidence_answers",
            "model": None,
            "question": question,
            "dossier_id": dossier["dossier_id"],
            "generated_at": dossier["generated_at"],
            "answer": lines,
            "evidence": dossier["evidence"],
            "can_trade": False,
            "disclaimer": "Rule-generated evidence answer, not an AI model forecast or evidence of profitable edge.",
        }
        body["answer_hash"] = digest(body)
        return body
