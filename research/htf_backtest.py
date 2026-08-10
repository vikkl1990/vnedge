"""Run htf_structure_break_v1 on the FROZEN selection (seen) window and score §5.

Seen window only (2023-01-01 → 2025-06-30). The sealed tail (2025-07 → 2026-06)
is NOT touched here — it is opened once, and only if §5 passes.
No parameters are tuned (the contract froze them), so this is out-of-sample by
construction; walk-forward selection is degenerate with a zero-free-param config.
"""
import sys

import pandas as pd

sys.path.insert(0, "src")
from vnedge.backtest.backtester import BacktestConfig, run_backtest
from vnedge.backtest.fee_model import FeeModel
from vnedge.backtest.slippage_model import SlippageModel
from vnedge.strategy.htf_structure_break import HtfStructureBreak

SEEN_START = pd.Timestamp("2023-01-01", tz="UTC")
SEEN_END = pd.Timestamp("2025-06-30 23:59", tz="UTC")
CFG = BacktestConfig(
    initial_equity_usd=500.0,
    max_holding_bars=12,  # 12h vertical barrier on 1h
    fees=FeeModel(taker_bps=5.0),      # 10 bps taker RT
    slippage=SlippageModel(bps=2.0),   # + 4 bps RT  => 14 bps modelled cost
)


def net(t):
    return t.gross_pnl_usd - t.fees_usd + t.funding_usd


def run_symbol(sym):
    c1 = pd.read_parquet(f"research/htf_data/{sym}_1h.parquet")
    c4 = pd.read_parquet(f"research/htf_data/{sym}_4h.parquet")
    for c in (c1, c4):
        c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
    s1 = c1[(c1.timestamp >= SEEN_START) & (c1.timestamp <= SEEN_END)].reset_index(drop=True)
    s4 = c4[(c4.timestamp >= SEEN_START) & (c4.timestamp <= SEEN_END)].reset_index(drop=True)
    strat = HtfStructureBreak(s4)
    res = run_backtest(s1, None, strat, CFG, symbol=f"{sym}/USDT:USDT", timeframe="1h")
    return list(res.trades), len(s1)


all_trades = []
per_market = {}
print(f"=== htf_structure_break_v1 · SEEN window {SEEN_START.date()} → {SEEN_END.date()} ===")
for sym in ["BTC", "ETH"]:
    trades, nbars = run_symbol(sym)
    m_net = sum(net(t) for t in trades)
    per_market[sym] = m_net
    all_trades += trades
    stops = sum(1 for t in trades if t.exit_reason == "stop")
    print(f"  {sym}: {len(trades):>4} trades over {nbars} bars · net ${m_net:+.2f} · "
          f"stops {stops} ({(stops/len(trades)*100 if trades else 0):.0f}%)")

n = len(all_trades)
gross = sum(t.gross_pnl_usd for t in all_trades)
fees = sum(t.fees_usd for t in all_trades)
net_total = sum(net(t) for t in all_trades)
wins = [net(t) for t in all_trades if net(t) > 0]
losses = [net(t) for t in all_trades if net(t) < 0]
pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
stops = sum(1 for t in all_trades if t.exit_reason == "stop")
fsr = stops / n if n else 0.0
by_reason = {}
for t in all_trades:
    by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

print("-" * 66)
print(f"  aggregate: {n} trades · gross ${gross:+.2f} · fees ${fees:.2f} · net ${net_total:+.2f}")
print(f"  PF={pf:.2f} · false-signal(stop)={fsr*100:.0f}% · exits={by_reason}")
print(f"  per-market net: " + ", ".join(f"{k} ${v:+.2f}" for k, v in per_market.items()))
print("-" * 66)


def ok(b):
    return "PASS" if b else "fail"


c1 = n >= 40
c2 = gross > fees
c3 = net_total > 0
c4 = pf >= 1.20
c5 = any(v > 0 for v in per_market.values())
c6 = fsr < 0.65
print("§5 selection criteria (ALL must pass to open the sealed tail):")
print(f"  [{ok(c1)}] >=40 trades ................ {n}")
print(f"  [{ok(c2)}] gross > costs ............. ${gross:+.2f} vs ${fees:.2f}")
print(f"  [{ok(c3)}] net expectancy > 0 ........ ${net_total:+.2f}")
print(f"  [{ok(c4)}] PF >= 1.20 ................. {pf:.2f}")
print(f"  [{ok(c5)}] >=1 market net-positive .... {ok(c5)}")
print(f"  [{ok(c6)}] false-signal rate < 65% .... {fsr*100:.0f}%")
print("=" * 66)
print("VERDICT:", "§5 PASSED — eligible to open the sealed tail (once)"
      if all([c1, c2, c3, c4, c5, c6]) else "§5 FAILED — hypothesis not supported on seen data")
