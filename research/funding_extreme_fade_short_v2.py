"""Sealed evaluation — funding_extreme_fade_short_v2 (Scanner Rework Protocol).

Research 2023-01-01→2025-06-30 = diagnostics only. Sealed tail 2025-07-01→
2026-06-30 = the ONLY pass/fail. One shot against the locked bar (prereg
docs/prereg/funding_extreme_fade_short_v2_20260812). Plans are resolved with the
plan-native ExitEngine — no old exit hacks. Conservative: no funding credit.

Run: python research/funding_extreme_fade_short_v2.py
"""
from __future__ import annotations

import pandas as pd

from vnedge.plan import CostModel, ExitEngine
from vnedge.plan.builders import FundingExtremeFadeShortV2Builder

CANDLES = "research/htf_data/BTC_1h.parquet"
FUNDING = "research/htf_data/BTC_funding.parquet"
RESEARCH = (pd.Timestamp("2023-01-01", tz="UTC"), pd.Timestamp("2025-06-30 23:59", tz="UTC"))
SEALED = (pd.Timestamp("2025-07-01", tz="UTC"), pd.Timestamp("2026-06-30 23:59", tz="UTC"))
RISK_PCT = 0.01          # 1% of running equity risked to the stop, per trade
START_EQUITY = 500.0


def _simulate(builder: FundingExtremeFadeShortV2Builder, df: pd.DataFrame,
              lo: pd.Timestamp, hi: pd.Timestamp) -> list[dict]:
    """One position at a time. Start trades only in [lo, hi]; resolve forward."""
    ts = pd.to_datetime(df["timestamp"], utc=True)
    n = len(df)
    trades: list[dict] = []
    busy_until = -1
    for i in range(builder.warmup_bars, n - 1):
        if not (lo <= ts.iloc[i] <= hi) or i <= busy_until:
            continue
        plan = builder.build_plan(df, i)
        if plan is None:
            continue
        entry = float(df["open"].iloc[i + 1])            # next_open
        ee = ExitEngine(plan, entry)
        gross = 0.0
        exit_i = i + 1
        for j in range(i + 1, n):
            r = df.iloc[j]
            for ev in ee.on_bar(float(r["high"]), float(r["low"]), float(r["close"]), j - i):
                gross += (entry - ev.price) / entry * 1e4 * ev.size_pct   # short; size_pct is a fraction
            if ee.closed:
                exit_i = j
                break
        net = gross - plan.costs.round_trip_bps          # fees + slip (14 bps)
        trades.append({
            "entry_ts": ts.iloc[i + 1], "exit_ts": ts.iloc[exit_i],
            "stop_bps": plan.risk.stop_bps, "tp1_bps": plan.tp1_bps,
            "gross_bps": gross, "net_bps": net,
            "r_multiple": net / plan.risk.stop_bps,      # net as a multiple of risk
        })
        busy_until = exit_i
    return trades


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    nets = [t["net_bps"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    # risk-based equity curve for a real DD%: P&L = risk$ × (net_bps / stop_bps)
    eq = START_EQUITY
    peak = eq
    max_dd = 0.0
    for t in trades:
        eq += (RISK_PCT * eq) * t["r_multiple"]
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    return {
        "trades": len(trades),
        "net_bps": round(sum(nets), 1),
        "mean_net_bps": round(sum(nets) / len(nets), 2),
        "profit_factor": round(pf, 3),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1),
        "max_dd_pct": round(100 * max_dd, 2),
        "final_equity": round(eq, 2),
        "worst_net_bps": round(min(nets), 1),
        "best_net_bps": round(max(nets), 1),
    }


def _verdict(m: dict) -> tuple[bool, list[str]]:
    fails = []
    if m.get("trades", 0) < 15:
        fails.append(f"trades {m.get('trades', 0)} < 15 (INCONCLUSIVE, not a pass)")
    if m.get("net_bps", -1) <= 0:
        fails.append(f"net_bps {m.get('net_bps')} <= 0")
    if m.get("profit_factor", 0) < 1.20:
        fails.append(f"PF {m.get('profit_factor')} < 1.20")
    if m.get("max_dd_pct", 100) > 15.0:
        fails.append(f"max_dd {m.get('max_dd_pct')}% > 15%")
    if m.get("mean_net_bps", -1) <= 0:
        fails.append(f"mean_net_bps {m.get('mean_net_bps')} <= 0")
    return (not fails, fails)


def main() -> None:
    candles = pd.read_parquet(CANDLES)
    funding = pd.read_parquet(FUNDING)
    builder = FundingExtremeFadeShortV2Builder(CostModel())     # swing world, 17bps rt
    df = builder.prepare(candles, funding)

    print("=" * 66)
    print("funding_extreme_fade_short_v2 — SEALED evaluation")
    print("=" * 66)
    for name, (lo, hi), sealed in [("RESEARCH (diagnostics only)", RESEARCH, False),
                                   ("SEALED TAIL (pass/fail)", SEALED, True)]:
        m = _metrics(_simulate(builder, df, lo, hi))
        print(f"\n[{name}]  {lo.date()} → {hi.date()}")
        for k, v in m.items():
            print(f"    {k:16} {v}")
        if sealed:
            ok, fails = _verdict(m)
            print("\n  VERDICT:", "PASS ✅" if ok else "FAIL ❌")
            for f in fails:
                print("    -", f)


if __name__ == "__main__":
    main()
