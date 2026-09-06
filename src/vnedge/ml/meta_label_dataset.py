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
META_COLUMNS = ["strategy", "symbol", "side", "entry_ts", "net_usd", "lane"]


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
    params: FeatureParams = FeatureParams(),
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
    *,
    tolerance_seconds: float = 900.0,
) -> tuple[pd.DataFrame, dict]:
    """Join trades to the EXACT feature vectors the runtime logged.

    This is the train/serve-skew-free path (correction-spec W5.1 -> W5.2): the
    label comes from the trade outcome, but the features come from the live
    feature log (``ml.feature_log``) — the same numbers the decision was made
    on — rather than being re-derived from candles. Re-derivation can silently
    diverge from what the runtime actually computed (a warmup edge, a params
    drift, a different candle source); this path cannot.

    Each trade matches the fired feature-log row for the same
    ``(strategy, symbol)`` whose ``bar_ts`` is the latest at or before the
    trade's entry within ``tolerance_seconds`` (a causal join — the arming bar
    precedes the fill). Unmatched trades are dropped and COUNTED, never guessed;
    read the summary before trusting a small dataset.

    Returns ``(dataframe, summary)`` with the same columns as
    :func:`build_meta_label_dataset` plus a ``feature_source`` marker.
    """
    if feature_log.empty:
        empty = pd.DataFrame(columns=FEATURE_COLUMNS + ["meta_label"] + META_COLUMNS)
        return empty, {"samples": 0, "win_rate": 0.0, "matched": 0,
                       "dropped_no_log_row": 0, "dropped_nan_feature": 0,
                       "by_strategy": {}, "feature_source": "log"}

    present = [c for c in FEATURE_COLUMNS if c in feature_log.columns]
    missing_cols = [c for c in FEATURE_COLUMNS if c not in feature_log.columns]
    if missing_cols:
        raise ValueError(
            f"feature log is missing {len(missing_cols)} contract column(s): "
            f"{missing_cols[:5]}{'...' if len(missing_cols) > 5 else ''}"
        )

    fired = feature_log[feature_log["decision"] == "fired"].copy()
    # Startup backfill rows are reconstructions, not live decisions; excluding
    # them keeps operational labels tied to real kernel-crossed fires (review
    # P2: operational labels require backfill=false).
    if "backfill" in fired.columns:
        fired = fired[~fired["backfill"].fillna(False).astype(bool)]
    fired["bar_ts_dt"] = pd.to_datetime(fired["bar_ts"], utc=True, errors="coerce")
    fired = fired.dropna(subset=["bar_ts_dt"])
    # Strongest shared key is the lane (it encodes exchange+symbol+timeframe+
    # strategy), then side — so the join cannot select the wrong lane, venue,
    # timeframe, or direction (review P1). Fall back to (strategy, symbol, side)
    # only when a lane id is absent on either side.
    has_lane = "lane" in fired.columns and fired["lane"].astype(str).str.len().gt(0).any()
    has_side = "side" in fired.columns
    if has_lane:
        key_cols = ["lane", "side"] if has_side else ["lane"]
    else:
        key_cols = ["strategy_id", "symbol", "side"] if has_side else ["strategy_id", "symbol"]
    grouped: dict[tuple, pd.DataFrame] = {}
    for key, part in fired.groupby(key_cols):
        grouped[tuple(str(k) for k in (key if isinstance(key, tuple) else (key,)))] = (
            part.sort_values("bar_ts_dt")
        )

    def _trade_key(trade: TradeOutcome) -> tuple:
        if has_lane and trade.lane:
            return (str(trade.lane), str(trade.side)) if has_side else (str(trade.lane),)
        base = (str(trade.strategy), str(trade.symbol))
        return (*base, str(trade.side)) if has_side else base

    tolerance = pd.Timedelta(seconds=float(tolerance_seconds))
    rows: list[dict] = []
    no_log_row = nan_feature = 0
    for trade in trades:
        part = grouped.get(_trade_key(trade))
        if part is None:
            no_log_row += 1
            continue
        entry = pd.Timestamp(trade.entry_ts)
        if entry.tzinfo is None:
            entry = entry.tz_localize("UTC")
        eligible = part[
            (part["bar_ts_dt"] <= entry) & (part["bar_ts_dt"] >= entry - tolerance)
        ]
        if eligible.empty:
            no_log_row += 1
            continue
        logged = eligible.iloc[-1]
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
        rows.append(row)

    frame = pd.DataFrame(rows, columns=FEATURE_COLUMNS + ["meta_label"] + META_COLUMNS)
    summary = {
        "samples": len(frame),
        "win_rate": float(frame["meta_label"].mean()) if len(frame) else 0.0,
        "matched": len(frame),
        "dropped_no_log_row": no_log_row,
        "dropped_nan_feature": nan_feature,
        "by_strategy": (
            frame.groupby("strategy").size().to_dict() if len(frame) else {}
        ),
        "feature_source": "log",
    }
    return frame, summary
