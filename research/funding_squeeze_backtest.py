"""Run funding_squeeze_continuation_v1 on the FROZEN seen window; score OFFENSIVE §5.

Seen window only (2023-01 → 2025-06). Sealed tail untouched unless §5 passes.
No params tuned (frozen contract) → out-of-sample by construction.
"""
import sys

import pandas as pd

sys.path.insert(0, "src")
from vnedge.backtest.backtester import BacktestConfig, run_backtest
from vnedge.backtest.fee_model import FeeModel
from vnedge.backtest.slippage_model import SlippageModel
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation

SEEN_START = pd.Timestamp("2023-01-01", tz="UTC")
SEEN_END = pd.Timestamp("2025-06-30 23:59", tz="UTC")
CFG = BacktestConfig(
    initial_equity_usd=500.0, max_holding_bars=48,
    fees=FeeModel(taker_bps=5.0), slippage=SlippageModel(bps=2.0),
)


def net(t):
    return t.gross_pnl_usd - t.fees_usd + t.funding_usd


def run_symbol(sym):
    c1 = pd.read_parquet(f"research/htf_data/{sym}_1h.parquet")
    fnd = pd.read_parquet(f"research/htf_data/{sym}_funding.parquet")
    for c in (c1, fnd):
        c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
    s1 = c1[(c1.timestamp >= SEEN_START) & (c1.timestamp <= SEEN_END)].reset_index(drop=True)
    fs = fnd[(fnd.timestamp >= SEEN_START - pd.Timedelta("40d")) & (fnd.timestamp <= SEEN_END)].reset_index(drop=True)
    strat = FundingSqueezeContinuation(fs)
    res = run_backtest(s1, fs, strat, CFG, symbol=f"{sym}/USDT:USDT", timeframe="1h")
    return list(res.trades)


all_trades, per_market = [], {}
print(f"=== funding_squeeze_continuation_v1 · SEEN {SEEN_START.date()} → {SEEN_END.date()} ===")
for sym in ["BTC", "ETH"]:
    trades = run_symbol(sym)
    per_market[sym] = sum(net(t) for t in trades)
    all_trades += trades
    print(f"  {sym}: {len(trades):>4} trades · net ${per_market[sym]:+.2f}")

n = len(all_trades)
if n == 0:
    print("  ZERO qualifying setups on the seen window (rare-event, like panic_reversal).")
    print("VERDICT: §5 FAILED (no trades) — hypothesis untestable as frozen; NOT tuned.")
    sys.exit()

nets = [net(t) for t in all_trades]
wins = [x for x in nets if x > 0]
losses = [x for x in nets if x < 0]
net_total = sum(nets)
pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
avg_win = sum(wins) / len(wins) if wins else 0.0
avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
payoff = (avg_win / avg_loss) if avg_loss else float("inf")
gross_win = sum(wins)
win_conc = (max(wins) / gross_win) if wins else 0.0
by_reason = {}
for t in all_trades:
    by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

print("-" * 66)
print(f"  aggregate: {n} trades · net ${net_total:+.2f} · PF={pf:.2f} · payoff={payoff:.2f}")
print(f"  win-conc={win_conc*100:.0f}% · win-rate={len(wins)/n*100:.0f}% · exits={by_reason}")
print(f"  per-market: " + ", ".join(f"{k} ${v:+.2f}" for k, v in per_market.items()))
print("-" * 66)


def ok(b):
    return "PASS" if b else "fail"


c1_ = n >= 15
c2_ = net_total > 0
c3_ = pf >= 1.25
c4_ = payoff >= 1.80
c5_ = win_conc <= 0.40
c6_ = any(v > 0 for v in per_market.values())
print("§5 OFFENSIVE_GATES (all must pass to open the sealed tail):")
print(f"  [{ok(c1_)}] >=15 trades ............. {n}")
print(f"  [{ok(c2_)}] net > 0 ................. ${net_total:+.2f}")
print(f"  [{ok(c3_)}] PF >= 1.25 ............. {pf:.2f}")
print(f"  [{ok(c4_)}] payoff >= 1.80 ......... {payoff:.2f}")
print(f"  [{ok(c5_)}] win-conc <= 40% ....... {win_conc*100:.0f}%")
print(f"  [{ok(c6_)}] >=1 market positive ... {ok(c6_)}")
print("=" * 66)
print("VERDICT:", "§5 PASSED — eligible to open the sealed tail (once)"
      if all([c1_, c2_, c3_, c4_, c5_, c6_]) else "§5 FAILED — hypothesis not supported on seen data")
