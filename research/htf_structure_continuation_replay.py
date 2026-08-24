"""Exploratory candle-proxy replay for HTF structure continuation.

The live scanner enters only after three distinct top-of-book observations
remain beyond the armed level for at least three seconds.  Binance does not
offer historical BBO through the kline endpoint used by this repository, so
this script does *not* claim executable-entry parity.  It reports two bounds:

``touch``
    The next one or two 15-minute bars merely trade through the armed level.
    Entry is booked at the level and same-bar stops win ties.  This is the
    deliberately generous setup-touch upper bound.

``close``
    The next one or two 15-minute bars must close beyond the level, within the
    live 8 bps chase cap.  Entry is booked at that close.  This is slower than
    the live three-second hold and therefore a conservative timing proxy, but
    it is still not historical quote evidence.

Both variants use the production scanner's causal 4h/1h context, 15m setup,
structure/ATR stop, deterioration rule, breakeven/trailing policy, 12-hour
cap, daily fire budget, and post-outcome cooldown.  Results are exploratory
and cannot promote the RESEARCH_ONLY scanner.

Usage::

    python -m research.htf_structure_continuation_replay --days 90
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Literal, TypedDict

import pandas as pd

from research.squeeze_trigger_replay import fetch
from vnedge.execution.exit_engine import ExitConfig, ExitDecision, ExitEngine
from vnedge.plan.cost_model import CostModel
from vnedge.strategy.realtime_entry import RealtimeEntryArm
from vnedge.strategy.realtime_scanners import HtfStructureContinuationRealtimeV1

UTC = dt.UTC
Proxy = Literal["touch", "close"]


@dataclass(frozen=True, slots=True)
class ReplayTrade:
    symbol: str
    proxy: Proxy
    side: str
    setup_ts: str
    entry_ts: str
    exit_ts: str
    entry: float
    exit: float
    stop: float
    held_bars: int
    reason: str
    gross_bps: float
    realized_cost_bps: float
    gate_cost_bps: float
    net_realized_bps: float
    net_gate_bps: float


class OpenMeta(TypedDict):
    side: str
    entry: float
    initial_stop: float
    entry_index: int
    entry_ts: str
    setup_ts: str


def _frame(symbol: str, bars: list[tuple]) -> pd.DataFrame:
    frame = pd.DataFrame(
        bars,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame["symbol"] = symbol
    frame["timeframe"] = "15m"
    frame["data_quality"] = "ok"
    frame["is_closed"] = True
    return frame


def _entry_candidate(
    row: pd.Series,
    arm: RealtimeEntryArm,
    proxy: Proxy,
) -> tuple[str, float] | None:
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    max_chase = HtfStructureContinuationRealtimeV1.acceptance_params.max_chase_bps
    if arm.allow_long:
        if proxy == "touch" and high > arm.long_level:
            return "long", arm.long_level
        chase = (close - arm.long_level) / arm.long_level * 10_000
        if proxy == "close" and close > arm.long_level and chase <= max_chase:
            return "long", close
    if arm.allow_short:
        if proxy == "touch" and low < arm.short_level:
            return "short", arm.short_level
        chase = (arm.short_level - close) / arm.short_level * 10_000
        if proxy == "close" and close < arm.short_level and chase <= max_chase:
            return "short", close
    return None


def _stop(arm: RealtimeEntryArm, side: str, entry: float) -> float:
    distance = HtfStructureContinuationRealtimeV1.acceptance_params.atr_stop_mult * arm.atr
    if side == "long":
        atr_stop = entry - distance
        structural = arm.long_structural_stop
        return min(atr_stop, structural) if structural is not None else atr_stop
    atr_stop = entry + distance
    structural = arm.short_structural_stop
    return max(atr_stop, structural) if structural is not None else atr_stop


def replay(
    symbol: str,
    prepared: pd.DataFrame,
    *,
    eval_start: pd.Timestamp,
    proxy: Proxy,
) -> list[ReplayTrade]:
    strategy = HtfStructureContinuationRealtimeV1()
    params = strategy.acceptance_params
    costs = CostModel.for_profile("delta_swing")
    exits = ExitEngine(
        ExitConfig(
            absolute_max_bars=48,
            max_age_bars=48,
            failed_breakout=False,
            breakeven_arm_r=strategy.realtime_breakeven_arm_r,
            trail_arm_r=strategy.realtime_trail_arm_r,
            trail_atr_mult=strategy.realtime_trail_atr_mult,
            breakeven_cost_bps=costs.round_trip_bps(include_safety=False),
        )
    )
    realized_cost = costs.round_trip_bps(include_safety=False)
    gate_cost = costs.round_trip_bps(include_safety=True)
    pending: RealtimeEntryArm | None = None
    open_meta: OpenMeta | None = None
    cooldown_until = -1
    current_day: dt.date | None = None
    fires_today = 0
    trades: list[ReplayTrade] = []

    def close_trade(decision: ExitDecision, index: int) -> None:
        nonlocal open_meta, cooldown_until
        if open_meta is None:
            return
        side = str(open_meta["side"])
        entry = float(open_meta["entry"])
        gross = (
            (decision.price / entry - 1.0)
            if side == "long"
            else (1.0 - decision.price / entry)
        ) * 10_000
        held = index - int(open_meta["entry_index"])
        net_gate = gross - gate_cost
        trades.append(
            ReplayTrade(
                symbol=symbol,
                proxy=proxy,
                side=side,
                setup_ts=str(open_meta["setup_ts"]),
                entry_ts=str(open_meta["entry_ts"]),
                exit_ts=prepared.iloc[index]["timestamp"].isoformat(),
                entry=entry,
                exit=decision.price,
                stop=float(open_meta["initial_stop"]),
                held_bars=held,
                reason=decision.reason,
                gross_bps=gross,
                realized_cost_bps=realized_cost,
                gate_cost_bps=gate_cost,
                net_realized_bps=gross - realized_cost,
                net_gate_bps=net_gate,
            )
        )
        cooldown_until = index + (
            params.cooldown_win_bars if net_gate > 0 else params.cooldown_loss_bars
        )
        open_meta = None

    for index, row in prepared.iterrows():
        ts = pd.Timestamp(row["timestamp"])
        day = ts.date()
        if day != current_day:
            current_day = day
            fires_today = 0

        if open_meta is not None:
            decision = exits.on_bar(
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                atr=float(row["hsc_atr"]),
                bar_index=index,
            )
            if decision is None:
                intent = strategy.exit_signal(
                    prepared,
                    index,
                    str(open_meta["side"]),
                    float(open_meta["entry"]),
                )
                if intent is not None:
                    decision = exits.close_now(
                        price=float(row["close"]),
                        reason=intent.reason,
                    )
            if decision is not None:
                close_trade(decision, index)
            if open_meta is not None:
                continue

        if ts >= eval_start and pending is not None:
            expired = index > pending.bar_index + pending.expires_after_bars
            if expired:
                pending = None
            elif index > pending.bar_index and index >= cooldown_until:
                candidate = _entry_candidate(row, pending, proxy)
                if candidate is not None and fires_today < params.max_fires_per_day:
                    side, entry = candidate
                    stop = _stop(pending, side, entry)
                    risk = entry - stop if side == "long" else stop - entry
                    if stop > 0 and risk > 0 and math.isfinite(risk):
                        exits.open_from_fire(
                            side=side,  # type: ignore[arg-type]
                            entry=entry,
                            stop=stop,
                            risk=risk,
                            box_edge=(pending.long_level if side == "long" else pending.short_level),
                            entry_bar=index,
                        )
                        open_meta = {
                            "side": side,
                            "entry": entry,
                            "initial_stop": stop,
                            "entry_index": index,
                            "entry_ts": ts.isoformat(),
                            "setup_ts": prepared.iloc[pending.bar_index]["timestamp"].isoformat(),
                        }
                        fires_today += 1
                        pending = None
                        # A touch proxy has no within-bar ordering evidence.
                        # Stop-first is the repository-wide pessimistic rule.
                        # Do not feed the complete bar to ExitEngine here: its
                        # favourable extreme may have occurred before entry,
                        # which would non-causally arm the trail/BE ratchet.
                        if proxy == "touch":
                            stop_hit = (
                                float(row["low"]) <= stop
                                if side == "long"
                                else float(row["high"]) >= stop
                            )
                            if stop_hit:
                                same_bar = exits.close_now(
                                    price=stop,
                                    reason="stop_same_bar_proxy",
                                )
                                if same_bar is not None:
                                    close_trade(same_bar, index)

        if open_meta is None and index >= strategy.warmup_bars:
            arm = strategy.realtime_arm(prepared, index)
            if arm is not None:
                pending = arm

    if open_meta is not None:
        last = len(prepared) - 1
        decision = exits.close_now(
            price=float(prepared.iloc[last]["close"]),
            reason="end_of_data",
        )
        if decision is not None:
            close_trade(decision, last)
    return trades


def _stats(rows: list[ReplayTrade]) -> dict[str, object]:
    gross = sum(row.gross_bps for row in rows)
    net_realized = sum(row.net_realized_bps for row in rows)
    net = sum(row.net_gate_bps for row in rows)
    wins = [row for row in rows if row.net_gate_bps > 0]
    win_mass = sum(row.net_gate_bps for row in wins)
    loss_mass = -sum(row.net_gate_bps for row in rows if row.net_gate_bps <= 0)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in rows:
        equity += row.net_gate_bps
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trades": len(rows),
        "wins": len(wins),
        "win_rate": (len(wins) / len(rows) if rows else None),
        "gross_bps": gross,
        "gross_per_trade_bps": (gross / len(rows) if rows else None),
        "net_realized_bps": net_realized,
        "net_gate_bps": net,
        "avg_net_bps": (net / len(rows) if rows else None),
        "profit_factor": (win_mass / loss_mass if loss_mass else None),
        "max_drawdown_bps": max_drawdown,
        "exits": dict(Counter(row.reason for row in rows)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    now_ms = int(dt.datetime.now(UTC).timestamp() * 1000)
    end_ms = now_ms - now_ms % 900_000
    eval_start = pd.Timestamp(end_ms - args.days * 86_400_000, unit="ms", tz="UTC")
    results: dict[str, dict[str, object]] = {}
    all_rows: dict[Proxy, list[ReplayTrade]] = {"touch": [], "close": []}
    for symbol in [value.strip().upper() for value in args.symbols.split(",") if value.strip()]:
        bars = fetch(symbol, "15m", end_ms - (args.days + 10) * 86_400_000, end_ms)
        strategy = HtfStructureContinuationRealtimeV1()
        prepared = strategy.prepare(_frame(symbol, bars)).reset_index(drop=True)
        symbol_stats: dict[str, object] = {
            "closed_15m_bars": len(prepared),
            "setups_in_window": int(
                prepared.loc[prepared["timestamp"].ge(eval_start), "rt_arm_ready"].eq(1).sum()
            ),
        }
        for proxy in ("touch", "close"):
            rows = replay(symbol, prepared, eval_start=eval_start, proxy=proxy)
            all_rows[proxy].extend(rows)
            symbol_stats[proxy] = _stats(rows)
        results[symbol] = symbol_stats
    results["combined"] = {proxy: _stats(rows) for proxy, rows in all_rows.items()}
    report_costs = CostModel.for_profile("delta_swing")
    payload = {
        "strategy_id": HtfStructureContinuationRealtimeV1.strategy_id,
        "status": "EXPLORATORY_BAR_PROXY_NOT_BBO_PARITY",
        "window_days": args.days,
        "window_end_utc": pd.Timestamp(end_ms, unit="ms", tz="UTC").isoformat(),
        "costs": {
            "profile": report_costs.profile,
            "realized_rt_bps": round(
                report_costs.round_trip_bps(include_safety=False), 3
            ),
            "gate_stress_rt_bps": round(
                report_costs.round_trip_bps(include_safety=True), 3
            ),
        },
        "results": results,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(json.dumps(payload, indent=2, sort_keys=True))
    for proxy, rows in all_rows.items():
        if rows:
            print(f"\n{proxy} trades")
            for row in rows:
                print(json.dumps(asdict(row), sort_keys=True))


if __name__ == "__main__":
    main()
