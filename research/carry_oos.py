"""xsect_carry_v1 — SEALED-TAIL test. Frozen construction, one shot.

Runs the locked carry book over the full period, then reports stats on the
UNTOUCHED tail (2025-07-01 → 2026-06-30) only. Seen slice reported for sanity.
"""
from pathlib import Path

import numpy as np
import pandas as pd

U = Path("research/universe")
SYMS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC"]
K, REBAL, COST_BPS = 3, 7, 14.0
SEEN_END = pd.Timestamp("2025-06-30", tz="UTC")
TAIL_START = pd.Timestamp("2025-07-01", tz="UTC")
TAIL_END = pd.Timestamp("2026-06-30", tz="UTC")
ANN = 365 ** 0.5

close, fund = {}, {}
for s in SYMS:
    c = pd.read_parquet(U / f"{s}_1d.parquet"); c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
    close[s] = c.set_index("timestamp")["close"]
    f = pd.read_parquet(U / f"{s}_funding.parquet"); f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True)
    fund[s] = f.set_index("timestamp")["funding_rate"].resample("1D").sum()
close = pd.DataFrame(close).sort_index()
fund = pd.DataFrame(fund).reindex(close.index).fillna(0.0)
ret = close.pct_change()
dates = close.index

daily, turns, w_prev = [], [], None
for t in range(31, len(dates) - 1):
    if (t - 31) % REBAL == 0:
        sc = fund.iloc[max(0, t - 3):t + 1].mean()
        if sc.isna().any():
            daily.append((dates[t + 1], 0.0)); continue
        order = sc.sort_values()                     # ascending: lowest funding first
        w = pd.Series(0.0, index=SYMS)
        w[order.index[:K]] = 1.0 / K                 # long low funding
        w[order.index[-K:]] = -1.0 / K               # short high funding
        turnover = (w - (w_prev if w_prev is not None else 0)).abs().sum()
        turns.append(turnover)
        cost = turnover * COST_BPS / 1e4
        w_prev = w
    else:
        cost = 0.0
    r_next = ret.iloc[t + 1].reindex(SYMS).fillna(0.0)
    f_next = fund.iloc[t + 1].reindex(SYMS).fillna(0.0)
    px = float((w_prev * r_next).sum())
    carry = float((-w_prev * f_next).sum())
    daily.append((dates[t + 1], px + carry - cost))

s = pd.Series({d: v for d, v in daily}).sort_index()


def stats(sl, label):
    if len(sl) < 5:
        print(f"  {label}: insufficient data"); return
    mean, sd = sl.mean(), sl.std()
    sharpe = mean / sd * ANN if sd > 0 else 0.0
    tstat = mean / sd * (len(sl) ** 0.5) if sd > 0 else 0.0
    curve = (1 + sl).cumprod()
    dd = float((curve / curve.cummax() - 1).min())
    print(f"  {label:24s} Sharpe {sharpe:+.2f} · t {tstat:+.2f} · ret {curve.iloc[-1]-1:+.1%} "
          f"· maxDD {dd:+.1%} · n={len(sl)}")


print("=== xsect_carry_v1 — frozen construction ===")
stats(s[s.index <= SEEN_END], "SEEN (2023-01→2025-06)")
tail = s[(s.index >= TAIL_START) & (s.index <= TAIL_END)]
print("  " + "-" * 62)
stats(tail, "SEALED TAIL (2025-07→2026-06)")
print(f"  avg weekly turnover: {np.mean(turns):.2f}")
print("=" * 66)

mean, sd = tail.mean(), tail.std()
sh = mean / sd * ANN if sd > 0 else 0.0
tt = mean / sd * (len(tail) ** 0.5) if sd > 0 else 0.0
tot = float((1 + tail).prod() - 1)
passed = sh >= 0.5 and tot > 0
print("OOS GATE (Sharpe>=0.5, return>0):",
      "PASSED — carry holds out-of-sample" if passed else "FAILED — carry did not survive the tail")
