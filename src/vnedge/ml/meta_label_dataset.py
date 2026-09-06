"""Meta-label dataset builder — the bridge from live trades to a training set.

Meta-labeling (role ① of the ML program): the rule-based signal decides
direction/entry; a secondary model learns P(this signal wins after costs). Its
training data is the REALIZED outcome of the primary signals, assembled here
from the paper/shadow decision journals plus the causal feature matrix at each
signal's entry bar.

Causality is the whole point: FEATURES come from `build_feature_matrix` at the
ENTRY bar (bars 0..entry only — causality unit-tested). The LABEL is the trade's
realized net, which of course looks forward — that is a label's job. No feature
ever sees the outcome, so the resulting dataset is leakage-free by construction.

Nothing here trains or trades; it produces a labeled DataFrame for the gated
promotion pipeline (validation → untouched judgment → shadow → paper → ladder).
Until the paper trials accumulate outcomes this returns few/zero rows — that is
expected, and honest.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix

#: journal record kinds that represent a CLOSED primary-signal trade
_OUTCOME_KINDS = ("shadow_outcome", "live_paper_exit", "tick_stop_exit")
_NET_FIELDS = ("virtual_net_usd", "net_usd", "realized_pnl_usd")

#: columns attached alongside the features + label for traceability / grouping
META_COLUMNS = [
    "strategy",
    "symbol",
    "side",
    "entry_ts",
    "net_usd",
    "lane",
    "decision_id",
]


@dataclass(frozen=True)
class TradeOutcome:
    """One closed primary-signal trade: features come from `entry_ts`, the label
    from `net_usd` (> 0 → the signal won after costs)."""

    strategy: str
    symbol: str
    side: str
    entry_ts: pd.Timestamp
    net_usd: float
    lane: str = ""
    decision_id: str = ""
    path_id: str = ""
    performance_eligible: bool = False


def _first_present(record: Mapping[str, Any], keys: Iterable[str]):
    for k in keys:
        v = record.get(k)
        if v is not None:
            return v
    return None


def parse_journal_trades(
    records: Iterable[Mapping[str, Any]], *, default_lane: str = ""
) -> list[TradeOutcome]:
    """Extract closed trades from decision-journal records.

    The primary signal's entry time is embedded in ``intent_key``
    (``strategy|symbol|side|entry_ms``), so each closed-trade record is
    self-contained — no fragile pairing of entry/exit rows required.

    ``default_lane`` labels each trade with the lane it came from when the record
    itself carries no ``lane`` field — callers pass the journal filename's lane id
    so trades can later be joined to that lane's exact-timeframe candle cache.
    """
    out: list[TradeOutcome] = []
    for record in records:
        if record.get("kind") not in _OUTCOME_KINDS:
            continue
        # Decision-journal rows nest their fields under `payload`; accept both a
        # nested record and a flat one (the fields fall back to the record).
        payload = record.get("payload")
        body = payload if isinstance(payload, dict) else record
        net = _first_present(body, _NET_FIELDS)
        if net is None:
            continue
        parts = str(body.get("intent_key") or "").split("|")
        if len(parts) < 4:
            continue
        strategy, ik_symbol, ik_side, entry_ms = parts[0], parts[1], parts[2], parts[3]
        try:
            entry_ts = pd.to_datetime(int(entry_ms), unit="ms", utc=True)
        except (TypeError, ValueError):
            continue
        symbol = str(body.get("symbol") or "").strip() or ik_symbol
        side = str(body.get("side") or "").strip() or ik_side
        out.append(
            TradeOutcome(
                strategy=strategy,
                symbol=symbol,
                side=side,
                entry_ts=entry_ts,
                net_usd=float(net),
                lane=str(record.get("lane") or body.get("lane") or default_lane),
                decision_id=str(body.get("decision_id") or body.get("intent_key") or ""),
                path_id=str(
                    body.get("path_id")
                    or (
                        body.get("execution_evidence", {}).get("path_id")
                        if isinstance(body.get("execution_evidence"), dict)
                        else ""
                    )
                    or ""
                ),
                performance_eligible=body.get("performance_eligible") is True,
            )
        )
    return out


def load_lane_journal_trades(lane_dir: Path | str) -> list[TradeOutcome]:
    """Read every ``*.journal.jsonl`` under `lane_dir` and parse its trades.

    Each file is parsed on its own so the lane id (the filename stem, e.g.
    ``alpha_stack_confluence_v1_bybit_btcusdt_shadow``) is stamped onto every
    trade — the records themselves omit it. That lane id matches the lane's
    warmup candle cache (``<lane_id>.candles.parquet``), letting the label
    builder join features at the trade's exact symbol *and* timeframe.
    """
    root = Path(lane_dir)
    out: list[TradeOutcome] = []
    for path in sorted(root.glob("*.journal.jsonl")):
        lane_id = path.name[: -len(".journal.jsonl")]
        records: list[dict] = []
        try:
            for line in path.read_text().splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
        out.extend(parse_journal_trades(records, default_lane=lane_id))
    return out


def build_meta_label_dataset(
    trades: Iterable[TradeOutcome],
    candles_by_symbol: Mapping[str, pd.DataFrame],
    *,
    candles_by_lane: Mapping[str, pd.DataFrame] | None = None,
    funding_by_symbol: Mapping[str, pd.DataFrame] | None = None,
    params: FeatureParams | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Join causal features at each trade's entry bar with its win/loss label.

    Returns ``(dataframe, summary)``. The dataframe has one row per usable trade
    with ``FEATURE_COLUMNS`` + ``meta_label`` (1.0 win / 0.0 loss) + META_COLUMNS.
    Trades are dropped (and counted) when their symbol has no candles, the entry
    bar isn't found, or any feature is still NaN (warmup). The summary reports
    sample count, win rate, drops, and per-strategy counts — read it before
    trusting a small dataset.

    ``candles_by_lane`` maps a lane id to that lane's own candles (the exact
    symbol *and* timeframe it traded). It is preferred over ``candles_by_symbol``
    per trade: a 5m lane and a 4h lane on the same symbol need different bars, and
    only the lane's own candles align a coarse-timeframe entry to its bar. The
    symbol map remains the fallback for trades from lanes with no candle cache.
    """
    params = params or FeatureParams()
    funding_by_symbol = funding_by_symbol or {}
    candles_by_lane = candles_by_lane or {}

    # Build each symbol's causal feature matrix once, indexed by timestamp.
    def _matrix(candles: pd.DataFrame, funding: pd.DataFrame | None) -> pd.DataFrame:
        fm = build_feature_matrix(candles, funding, params)
        return fm.set_index(pd.DatetimeIndex(pd.to_datetime(fm["timestamp"], utc=True)))

    feats: dict[str, pd.DataFrame] = {
        symbol: _matrix(candles, funding_by_symbol.get(symbol))
        for symbol, candles in candles_by_symbol.items()
    }
    # Per-lane matrices carry no funding history (caches are OHLCV only); that is
    # the same footing the symbol lake runs on, which already yields labels.
    feats_by_lane: dict[str, pd.DataFrame] = {
        lane: _matrix(candles, None) for lane, candles in candles_by_lane.items()
    }

    rows: list[dict] = []
    no_symbol = no_bar = nan_feature = 0
    for trade in trades:
        fm = feats_by_lane.get(trade.lane) if trade.lane else None
        if fm is None:
            fm = feats.get(trade.symbol)
        if fm is None:
            no_symbol += 1
            continue
        if trade.entry_ts not in fm.index:
            no_bar += 1
            continue
        feature_row = fm.loc[trade.entry_ts, FEATURE_COLUMNS]
        if feature_row.isna().any():
            nan_feature += 1
            continue
        row = {col: float(feature_row[col]) for col in FEATURE_COLUMNS}
        row["meta_label"] = 1.0 if trade.net_usd > 0 else 0.0
        row["strategy"] = trade.strategy
        row["symbol"] = trade.symbol
        row["side"] = trade.side
        row["entry_ts"] = trade.entry_ts
        row["net_usd"] = trade.net_usd
        row["lane"] = trade.lane
        row["decision_id"] = trade.decision_id
        rows.append(row)

    frame = pd.DataFrame(rows, columns=FEATURE_COLUMNS + ["meta_label"] + META_COLUMNS)
    summary = {
        "samples": len(frame),
        "win_rate": float(frame["meta_label"].mean()) if len(frame) else 0.0,
        "dropped_no_symbol": no_symbol,
        "dropped_no_bar": no_bar,
        "dropped_nan_feature": nan_feature,
        "by_strategy": (
            frame.groupby("strategy").size().to_dict() if len(frame) else {}
        ),
    }
    return frame, summary


def build_meta_label_dataset_from_log(
    trades: Iterable[TradeOutcome],
    feature_log: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    """Join trades to the EXACT feature vectors the runtime logged.

    This is the train/serve-skew-free path (correction-spec W5.1 -> W5.2): the
    label comes from the trade outcome, but the features come from the live
    feature log (``ml.feature_log``) — the same numbers the decision was made
    on — rather than being re-derived from candles. Re-derivation can silently
    diverge from what the runtime actually computed (a warmup edge, a params
    drift, a different candle source); this path cannot.

    Each operational outcome matches exactly one fired feature-log row by
    ``decision_id``. Approximate timestamp/lane/side joins are forbidden: two
    arms can share those values and still represent different immutable bars
    or permission snapshots. Outcomes without the kernel path and explicit
    performance eligibility are research diagnostics, not labels.

    Returns ``(dataframe, summary)`` with the same columns as
    :func:`build_meta_label_dataset` plus a ``feature_source`` marker.
    """
    if feature_log.empty:
        empty = pd.DataFrame(columns=FEATURE_COLUMNS + ["meta_label"] + META_COLUMNS)
        return empty, {
            "samples": 0,
            "win_rate": 0.0,
            "matched": 0,
            "dropped_no_log_row": 0,
            "dropped_missing_decision_id": 0,
            "dropped_ineligible_outcome": 0,
            "dropped_identity_mismatch": 0,
            "dropped_duplicate_outcome": 0,
            "dropped_nan_feature": 0,
            "by_strategy": {},
            "feature_source": "log",
        }

    present = [c for c in FEATURE_COLUMNS if c in feature_log.columns]
    missing_cols = [c for c in FEATURE_COLUMNS if c not in feature_log.columns]
    if missing_cols:
        raise ValueError(
            f"feature log is missing {len(missing_cols)} contract column(s): "
            f"{missing_cols[:5]}{'...' if len(missing_cols) > 5 else ''}"
        )

    if "decision_id" not in feature_log.columns:
        raise ValueError("feature log is missing decision_id")
    fired = feature_log[feature_log["decision"] == "fired"].copy()
    # Startup backfill rows are reconstructions, not live decisions; excluding
    # them keeps operational labels tied to real kernel-crossed fires (review
    # P2: operational labels require backfill=false).
    if "backfill" in fired.columns:
        fired = fired[~fired["backfill"].fillna(False).astype(bool)]
    fired["decision_id"] = fired["decision_id"].fillna("").astype(str)
    fired = fired[fired["decision_id"].str.len().gt(0)]
    duplicates = fired["decision_id"].duplicated(keep=False)
    if duplicates.any():
        duplicate_ids = sorted(fired.loc[duplicates, "decision_id"].unique())
        raise ValueError(
            "feature log contains duplicate decision_id row(s): "
            + ", ".join(duplicate_ids[:5])
        )
    by_decision_id = fired.set_index("decision_id", drop=False)

    rows: list[dict] = []
    no_log_row = missing_decision_id = ineligible = identity_mismatch = 0
    duplicate_outcome = nan_feature = 0
    used_decisions: set[str] = set()
    for trade in trades:
        decision_id = str(trade.decision_id).strip()
        if not decision_id:
            missing_decision_id += 1
            continue
        if trade.path_id != "kernel_v1" or not trade.performance_eligible:
            ineligible += 1
            continue
        if decision_id in used_decisions:
            duplicate_outcome += 1
            continue
        if decision_id not in by_decision_id.index:
            no_log_row += 1
            continue
        logged = by_decision_id.loc[decision_id]
        identity_fields = (
            ("strategy_id", trade.strategy),
            ("symbol", trade.symbol),
            ("side", trade.side),
        )
        if any(
            column in logged.index
            and str(logged[column]).strip()
            and str(logged[column]) != str(expected)
            for column, expected in identity_fields
        ):
            identity_mismatch += 1
            continue
        if (
            trade.lane
            and "lane" in logged.index
            and str(logged["lane"]).strip()
            and str(logged["lane"]) != trade.lane
        ):
            identity_mismatch += 1
            continue
        feature_values = logged[present]
        if feature_values.isna().any():
            nan_feature += 1
            continue
        row = {col: float(feature_values[col]) for col in FEATURE_COLUMNS}
        row["meta_label"] = 1.0 if trade.net_usd > 0 else 0.0
        row["strategy"] = trade.strategy
        row["symbol"] = trade.symbol
        row["side"] = trade.side
        row["entry_ts"] = trade.entry_ts
        row["net_usd"] = trade.net_usd
        row["lane"] = trade.lane
        row["decision_id"] = decision_id
        rows.append(row)
        used_decisions.add(decision_id)

    frame = pd.DataFrame(rows, columns=FEATURE_COLUMNS + ["meta_label"] + META_COLUMNS)
    summary = {
        "samples": len(frame),
        "win_rate": float(frame["meta_label"].mean()) if len(frame) else 0.0,
        "matched": len(frame),
        "dropped_no_log_row": no_log_row,
        "dropped_missing_decision_id": missing_decision_id,
        "dropped_ineligible_outcome": ineligible,
        "dropped_identity_mismatch": identity_mismatch,
        "dropped_duplicate_outcome": duplicate_outcome,
        "dropped_nan_feature": nan_feature,
        "by_strategy": (
            frame.groupby("strategy").size().to_dict() if len(frame) else {}
        ),
        "feature_source": "log",
    }
    return frame, summary
