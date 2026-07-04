"""Continuous research loop — the slow loop, made continuous.

    python -m vnedge.research.continuous_research

Every cycle (default hourly — walk-forward output only changes when a new
candle lands):

  1. refresh candles + funding via REST (incremental; full 365d backfill on
     first run), through the same quality gate as every other ingest
  2. re-run walk-forward for every registered strategy family x symbol on
     the rolling window
  3. evaluate promotion gates (sparse gates for event strategies, standard
     otherwise) and publish verdicts to research/live_research/latest.json
     (atomic) + an append-only feed.jsonl

Governance: this process has NO access to execution. It shares nothing with
the trial container except read-only market data and the research output
directory. Gates are the same frozen objects the judgment runs use; a PASS
here is a *candidate* signal prompting a proper pre-registered judgment on
untouched data — never an auto-promotion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from vnedge.backtest.backtester import BacktestConfig
from vnedge.backtest.walk_forward import (
    OFFENSIVE_GATES,
    SPARSE_STRATEGY_GATES,
    PromotionGates,
    WalkForwardResult,
    evaluate_promotion,
    param_grid,
    walk_forward,
)
from vnedge.data.candle_ingestor import ingest_candles
from vnedge.data.ccxt_client import CcxtPublicClient
from vnedge.data.funding_ingestor import ingest_funding
from vnedge.data.parquet_store import ParquetStore
from vnedge.research.strategy_diagnostics import diagnose
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.trend_continuation import TrendContinuation
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout

logger = logging.getLogger(__name__)

EXCHANGE = "binanceusdm"
# All non-BTC symbols are research-only: no paper deployment without gates
# + human approval. Offensive lanes (milestone 10A) sweep the wider set.
SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    "BNB/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT",
]
TIMEFRAME = "1h"
LOOKBACK_DAYS = 365
INTERVAL_SECONDS = float(os.environ.get("RESEARCH_INTERVAL_SECONDS", "3600"))
OUT_DIR = Path("research/live_research")

# The strategy/symbol pair currently running in the governed paper trial —
# drift detection watches THIS series and alerts the operator. It never
# mutates the trial.
TRIAL_STRATEGY = "funding_mean_reversion_v1"
TRIAL_SYMBOL = "BTC/USDT:USDT"
DRIFT_CONSECUTIVE_REJECTS = 3


def side_attribution(result: WalkForwardResult) -> dict:
    """Break OOS performance by trade side. For funding-MR this doubles as
    funding-sign attribution: shorts fade positive-funding crowding, longs
    fade negative. A PASS carried by one side is a pre-registration prompt
    for a side-specific variant — never an in-place tweak."""
    out = {}
    for side in ("long", "short"):
        trades = [
            t for w in result.windows for t in w.test_trades if t.side == side
        ]
        wins = sum(1 for t in trades if t.net_pnl_usd > 0)
        out[side] = {
            "trades": len(trades),
            "net_usd": round(sum(t.net_pnl_usd for t in trades), 2),
            "win_rate_pct": round(wins / len(trades) * 100.0, 1) if trades else 0.0,
        }
    return out


def wf_record(
    strategy: str, symbol: str, result: WalkForwardResult, gates: PromotionGates,
    gates_label: str = "standard",
) -> dict:
    decision = evaluate_promotion(result, gates)
    trades = sum(w.test_metrics.num_trades for w in result.windows)
    traded = sum(1 for w in result.windows if w.test_metrics.num_trades > 0)
    all_trades = [t for w in result.windows for t in w.test_trades]
    total_fees = round(sum(t.fees_usd for t in all_trades), 2)
    wins = [t.net_pnl_usd for t in all_trades if t.net_pnl_usd > 0]
    losses = [-t.net_pnl_usd for t in all_trades if t.net_pnl_usd <= 0]
    payoff = round(
        (sum(wins) / len(wins)) / (sum(losses) / len(losses)), 2
    ) if wins and losses else 0.0
    return {
        "attribution": side_attribution(result),
        "gates": gates_label,
        "strategy": strategy,
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "windows": len(result.windows),
        "traded_windows": traded,
        "oos_trades": trades,
        "oos_net_usd": round(result.oos_net_profit_usd, 2),
        "profitable_windows_pct": round(result.oos_profitable_window_pct, 1),
        "total_fees_usd": total_fees,
        "payoff_ratio": payoff,
        "verdict": "PASS" if decision.passed else "REJECT",
        "reasons": list(decision.reject_reasons),
        "updated": datetime.now(UTC).isoformat(),
    }


def read_feed_series(strategy: str, symbol: str, limit: int = 48) -> list[dict]:
    """Prior cycles' records for one strategy/symbol, oldest first."""
    path = OUT_DIR / "feed.jsonl"
    if not path.exists():
        return []
    series = []
    for line in path.read_text().strip().splitlines()[-limit:]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        for r in payload.get("results", []):
            if r.get("strategy") == strategy and r.get("symbol") == symbol:
                series.append(r)
    return series


def compute_drift_alerts(prev: list[dict], current: dict) -> list[dict]:
    """Edge-triggered drift detection for the live-trial strategy's rolling
    profile. Fires on TRANSITIONS (so hourly cycles don't re-alarm), and
    protects the trial by informing the operator — never by mutating it."""
    alerts: list[dict] = []
    now = datetime.now(UTC).isoformat()

    def alert(rule: str, severity: str, message: str) -> None:
        alerts.append({"ts": now, "rule_id": rule, "severity": severity,
                       "message": message, "mode": "research"})

    last = prev[-1] if prev else None
    if last:
        if last["verdict"] == "PASS" and current["verdict"] == "REJECT":
            alert("drift_verdict_flip", "warning",
                  f"rolling verdict flipped PASS->REJECT for "
                  f"{current['strategy']} {current['symbol']}: "
                  + "; ".join(current["reasons"][:3]))
        if last["oos_net_usd"] > 0 >= current["oos_net_usd"]:
            alert("drift_oos_sign_flip", "warning",
                  f"rolling OOS net turned negative: ${current['oos_net_usd']:+.2f} "
                  f"(was ${last['oos_net_usd']:+.2f})")

    trade_counts = [r["oos_trades"] for r in prev if r["oos_trades"] > 0]
    if len(trade_counts) >= 6:
        median = sorted(trade_counts)[len(trade_counts) // 2]
        collapsed = current["oos_trades"] < 0.5 * median
        was_collapsed = last is not None and last["oos_trades"] < 0.5 * median
        if collapsed and not was_collapsed:
            alert("drift_trade_collapse", "warning",
                  f"OOS trade count collapsed: {current['oos_trades']} vs "
                  f"trailing median {median}")

    rejects = 0
    for r in reversed(prev + [current]):
        if r["verdict"] == "REJECT":
            rejects += 1
        else:
            break
    if rejects == DRIFT_CONSECUTIVE_REJECTS:  # fires exactly once at the threshold
        alert("drift_consecutive_rejects", "critical",
              f"{rejects} consecutive rolling REJECT cycles for "
              f"{current['strategy']} {current['symbol']} — review the live "
              f"paper trial for regime decay (do not tune mid-trial)")
    return alerts


async def refresh_data(store: ParquetStore, symbol: str) -> bool:
    """Incremental refresh through the quality gate; full backfill if the
    store is empty. Returns False when the gate rejects (research skips the
    symbol this cycle — fail closed, never research on bad data)."""
    until_ms = int(time.time() * 1000)
    try:
        candles = store.read_candles(EXCHANGE, symbol, TIMEFRAME)
        since_ms = int(candles["timestamp"].iloc[-1].value // 1_000_000) - 2 * 3_600_000
    except FileNotFoundError:
        since_ms = until_ms - LOOKBACK_DAYS * 86_400_000
        logger.info("%s: no local data — full %dd backfill", symbol, LOOKBACK_DAYS)

    # Funding prints only every 8h: a short incremental window would fetch
    # zero rows and (correctly) fail the empty-dataset gate. Always look
    # back >= 48h for funding; the upsert dedupes the overlap.
    funding_since_ms = min(since_ms, until_ms - 48 * 3_600_000)
    async with CcxtPublicClient(EXCHANGE) as client:
        c = await ingest_candles(
            client, store, symbol=symbol, timeframe=TIMEFRAME,
            since_ms=since_ms, until_ms=until_ms,
        )
        f = await ingest_funding(
            client, store, symbol=symbol,
            since_ms=funding_since_ms, until_ms=until_ms,
        )
    if not (c.persisted and f.persisted):
        logger.error("%s: quality gate rejected refresh (%s / %s)",
                     symbol, c.report.summary, f.report.summary)
        return False
    return True


def run_walk_forwards(store: ParquetStore, symbol: str) -> list[dict]:
    candles = store.read_candles(EXCHANGE, symbol, TIMEFRAME)
    funding = store.read_funding(EXCHANGE, symbol)
    cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=LOOKBACK_DAYS)
    c = candles[candles["timestamp"] >= cutoff].reset_index(drop=True)
    f = funding[funding["timestamp"] >= cutoff].reset_index(drop=True)
    config = BacktestConfig()
    records = []

    lanes = [
        ("funding_mean_reversion_v1",
         lambda **p: FundingMeanReversion(funding=f, **p),
         param_grid(extreme_pct=[0.85, 0.95], z_entry=[1.5, 2.5]),
         720, SPARSE_STRATEGY_GATES, "sparse"),
        ("trend_continuation_v1",
         lambda **p: TrendContinuation(funding=f, **p),
         param_grid(breakout_bars=[48, 96], take_profit_r=[2.0, 3.0]),
         360, PromotionGates(), "standard"),
        # --- offensive lanes (milestone 10A): research-only ---
        ("volatility_expansion_breakout_v1",
         lambda **p: VolatilityExpansionBreakout(funding=f, **p),
         param_grid(breakout_bars=[48, 96]),
         720, OFFENSIVE_GATES, "offensive"),
        ("panic_reversal_v1",
         lambda **p: PanicReversal(funding=f, **p),
         param_grid(drop_z_entry=[-2.5, -3.0]),
         720, OFFENSIVE_GATES, "offensive"),
        ("funding_squeeze_continuation_v1",
         lambda **p: FundingSqueezeContinuation(funding=f, **p),
         param_grid(extreme_pct=[0.88, 0.94]),
         720, OFFENSIVE_GATES, "offensive"),
    ]
    for name, factory, grid, test_bars, gates, label in lanes:
        result = walk_forward(
            c, f, factory, grid, config,
            train_bars=1440, test_bars=test_bars, symbol=symbol, timeframe=TIMEFRAME,
        )
        records.append(wf_record(name, symbol, result, gates, gates_label=label))
    return records


_GATES = {
    "sparse": SPARSE_STRATEGY_GATES,
    "offensive": OFFENSIVE_GATES,
    "standard": PromotionGates(),
}


def _build_strategy(strategy_id: str, funding, **params):
    from vnedge.strategy.strategy_registry import STRATEGIES

    return STRATEGIES[strategy_id](funding, **params)


def _promotion_distance(record: dict) -> float:
    """Lower = closer to passing. Positive-net lanes with few failed gates
    are the highest-value places to spend an auto-explore slot."""
    net_penalty = 0.0 if record["oos_net_usd"] > 0 else 1000.0 - record["oos_net_usd"]
    return net_penalty + 10.0 * len(record.get("reasons", []))


def _load_auto_state() -> dict:
    path = OUT_DIR / "auto_explore.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {"tried": [], "total_attempts": 0}


def _save_auto_state(state: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "auto_explore.json").write_text(json.dumps(state, indent=2))


def auto_explore(store: ParquetStore, records: list[dict], max_variants: int = 2) -> list[dict]:
    """Bounded auto-uplift: for the closest-to-passing failing lanes, run
    their top diagnostic suggestion as an EXPLORATORY variant. Results are
    labeled auto=true; a rolling PASS here is a candidate, never a promotion.
    Already-tried variants are skipped so the search can't inflate the
    multiple-comparisons count by re-running the same idea hourly."""
    state = _load_auto_state()
    tried = set(state["tried"])
    config = BacktestConfig()
    variants: list[dict] = []

    candidates = [r for r in records if r["verdict"] == "REJECT" and not r.get("auto")]
    candidates.sort(key=_promotion_distance)

    for base in candidates:
        if len(variants) >= max_variants:
            break
        diag = diagnose(base)
        for sug in diag.suggestions:
            key = f"{sug.variant_id}@{base['symbol']}"
            if key in tried:
                continue
            candles = store.read_candles(EXCHANGE, base["symbol"], TIMEFRAME)
            funding = store.read_funding(EXCHANGE, base["symbol"])
            cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=LOOKBACK_DAYS)
            c = candles[candles["timestamp"] >= cutoff].reset_index(drop=True)
            f = funding[funding["timestamp"] >= cutoff].reset_index(drop=True)
            factory = (lambda sug=sug, f=f: (
                lambda **p: _build_strategy(sug.strategy_id, f, **{**sug.fixed_params, **p})
            ))()
            grid = param_grid(**sug.grid_axes) if sug.grid_axes else [{}]
            try:
                result = walk_forward(
                    c, f, factory, grid, config, train_bars=1440,
                    test_bars=sug.test_bars, symbol=base["symbol"], timeframe=TIMEFRAME,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto-explore %s failed: %s", key, exc)
                tried.add(key)
                continue
            rec = wf_record(sug.variant_id, base["symbol"], result,
                            _GATES[sug.gates_label], gates_label=sug.gates_label)
            rec["auto"] = True
            rec["parent"] = base["strategy"]
            rec["goal"] = sug.goal
            rec["rationale"] = sug.rationale
            variants.append(rec)
            tried.add(key)
            state["total_attempts"] += 1
            logger.info("auto-explore %s: %s (oos $%+.2f) — %s",
                        key, rec["verdict"], rec["oos_net_usd"], sug.goal)
            break  # one variant per base lane per cycle

    state["tried"] = sorted(tried)
    _save_auto_state(state)
    return variants


def publish(records: list[dict], started: float,
            drift_alerts: list[dict] | None = None,
            auto_state: dict | None = None) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cycle_seconds": round(time.time() - started, 1),
        "lookback_days": LOOKBACK_DAYS,
        "note": "rolling exploratory walk-forward — a PASS is a candidate "
                "signal, not a promotion; judgment requires untouched data. "
                "auto=true rows are machine-proposed uplift variants, "
                "exploratory only.",
        "results": records,
        "drift_alerts": drift_alerts or [],
        "auto_explore": {
            "total_attempts": (auto_state or {}).get("total_attempts", 0),
            "distinct_variants": len((auto_state or {}).get("tried", [])),
        },
    }
    tmp = OUT_DIR / "latest.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(OUT_DIR / "latest.json")
    with open(OUT_DIR / "feed.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


async def run_cycle() -> list[dict]:
    started = time.time()
    store = ParquetStore("data")
    records: list[dict] = []
    for symbol in SYMBOLS:
        try:
            if not await refresh_data(store, symbol):
                continue
            records.extend(run_walk_forwards(store, symbol))
        except Exception as exc:  # noqa: BLE001 — one symbol must not kill the loop
            logger.exception("research cycle failed for %s: %s", symbol, exc)

    # Drift watch on the live-trial strategy (read feed BEFORE publishing
    # this cycle, so `prev` excludes the current record).
    drift: list[dict] = []
    current = next((r for r in records if r["strategy"] == TRIAL_STRATEGY
                    and r["symbol"] == TRIAL_SYMBOL), None)
    if current is not None:
        drift = compute_drift_alerts(
            read_feed_series(TRIAL_STRATEGY, TRIAL_SYMBOL), current
        )
        if drift:
            from vnedge.monitoring.notifiers import LogNotifier, TelegramNotifier

            notifiers = [LogNotifier()]
            telegram = TelegramNotifier.from_env()
            if telegram is not None:
                notifiers.append(telegram)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            with open(OUT_DIR / "alerts.jsonl", "a", encoding="utf-8") as fh:
                for a in drift:
                    fh.write(json.dumps(a) + "\n")
                    for n in notifiers:
                        try:
                            n.send(a)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("drift notifier failed: %s", exc)

    # Attach failure diagnoses to every REJECT (cheap, always on): "why did
    # it fail, and what bounded variant might uplift it".
    for r in records:
        if r["verdict"] == "REJECT":
            diag = diagnose(r)
            r["diagnosis"] = {
                "failure_tags": list(diag.failure_tags),
                "notes": list(diag.notes),
                "suggested_variants": [s.variant_id for s in diag.suggestions],
            }

    # Bounded auto-explore: run the top suggestion for the closest-to-passing
    # lanes as exploratory variants. Never promotes, never touches the trial.
    try:
        variants = auto_explore(store, records)
        records.extend(variants)
    except Exception as exc:  # noqa: BLE001 — auto-explore must never kill a cycle
        logger.exception("auto-explore failed: %s", exc)

    publish(records, started, drift, _load_auto_state())
    for r in records:
        tag = " [auto]" if r.get("auto") else ""
        logger.info("%s %s: %s (oos $%+.2f, %d trades, %d windows)%s",
                    r["strategy"], r["symbol"], r["verdict"],
                    r["oos_net_usd"], r["oos_trades"], r["windows"], tag)
    return records


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logger.info("continuous research loop: %s every %.0fs", SYMBOLS, INTERVAL_SECONDS)
    while True:
        try:
            await run_cycle()
        except Exception as exc:  # noqa: BLE001
            logger.exception("cycle crashed: %s", exc)
        await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
