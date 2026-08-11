"""Cross-sectional (market-neutral) factor test — the framing the bot never tried.

Single-asset directional prediction is what failed (4x this session). This tests
RELATIVE value: each week, rank a 10-perp universe by a factor, go long the top,
short the bottom, dollar-neutral. That removes the direction-prediction problem
AND the dominant BTC-beta noise. Cost-aware. Three canonical crypto factors:
  * carry     — long low-funding, short high-funding (collect the funding spread)
  * momentum  — long past winners, short past losers (cross-sectional momentum)
  * reversal  — long past losers, short winners (short-term reversal)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

U = Path("research/universe")
SYMS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC"]
K = 3                      # legs per side (3 long, 3 short)
REBAL = 7                  # weekly rebalance (days)
COST_BPS = 14.0            # per-side round-trip on the fraction turned over
SEEN_END = pd.Timestamp("2025-06-30", tz="UTC")  # keep 2025-07→2026-06 sealed

# --- load wide frames -------------------------------------------------------
close, fund = {}, {}
for s in SYMS:
    c = pd.read_parquet(U / f"{s}_1d.parquet")
    c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
    close[s] = c.set_index("timestamp")["close"]
    f = pd.read_parquet(U / f"{s}_funding.parquet")
    f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True)
    fd = f.set_index("timestamp")["funding_rate"].resample("1D").sum()  # daily funding
    fund[s] = fd
close = pd.DataFrame(close).sort_index()
fund = pd.DataFrame(fund).reindex(close.index).fillna(0.0)
close = close[close.index <= SEEN_END]
fund = fund.reindex(close.index).fillna(0.0)
ret = close.pct_change()

dates = close.index
ANN = 365 ** 0.5


def factor_scores(name, t):
    """Score each symbol at day t using ONLY data up to t (causal)."""
    if name == "carry":
        return fund.iloc[max(0, t - 3):t + 1].mean()           # trailing funding
    if name == "momentum":
        if t < 30:
            return None
        return close.iloc[t] / close.iloc[t - 30] - 1.0          # 30d return
    if name == "reversal":
        if t < 7:
            return None
        return close.iloc[t] / close.iloc[t - 7] - 1.0           # 7d return
    return None


def run(name):
    long_hi = name == "momentum"          # momentum longs winners; carry/reversal long the LOW
    daily, w_prev = [], None
    for t in range(31, len(dates) - 1):
        if (t - 31) % REBAL == 0:
            sc = factor_scores(name, t)
            if sc is None or sc.isna().any():
                daily.append(0.0); continue
            order = sc.sort_values(ascending=not long_hi)       # winners first if long_hi
            longs, shorts = order.index[:K], order.index[-K:]
            w = pd.Series(0.0, index=SYMS)
            w[longs] = 1.0 / K
            w[shorts] = -1.0 / K
            turnover = (w - (w_prev if w_prev is not None else 0)).abs().sum()
            cost = turnover * COST_BPS / 1e4
            w_prev = w
        else:
            cost = 0.0
        # next-day return of the held book + funding carry (shorts collect + funding)
        r_next = ret.iloc[t + 1].reindex(SYMS).fillna(0.0)
        f_next = fund.iloc[t + 1].reindex(SYMS).fillna(0.0)
        px = float((w_prev * r_next).sum())
        carry = float((-w_prev * f_next).sum())                 # short(+w<0) collects +funding
        daily.append(px + carry - cost)
    d = pd.Series(daily)
    mean, sd = d.mean(), d.std()
    sharpe = (mean / sd * ANN) if sd > 0 else 0.0
    tstat = (mean / sd * (len(d) ** 0.5)) if sd > 0 else 0.0
    tot = (1 + d).prod() - 1
    return sharpe, tstat, tot, len(d)


print(f"=== cross-sectional factors · 10 perps · weekly · SEEN 2023-01→2025-06 ===")
print(f"{'factor':10s} {'net Sharpe':>11s} {'t-stat':>8s} {'total ret':>10s}  verdict")
print("-" * 56)
for name in ["carry", "momentum", "reversal"]:
    sh, tt, tot, n = run(name)
    verdict = "PROMISING" if (sh > 0.8 and abs(tt) > 2.0) else "no edge"
    print(f"{name:10s} {sh:>+11.2f} {tt:>+8.2f} {tot:>+9.1%}  {verdict}")
print("-" * 56)
print("(net = after 14bps turnover cost; carry includes funding collection;")
print(" a real cross-sectional edge = net Sharpe > ~0.8 with |t| > 2 on seen data)")
