# ST4H and DC20 — independent exploratory replay

BTCUSD, Delta India official venue OHLC, Jan 1 2025 through Sep 19 2026
(end exclusive Sep 20). Nov 2024 onward indicator warmup. Zero missing bars
in both downloaded windows. This is not canonical trade-lake or execution proof.

Each scanner ran in a separate process with its own position state and output
directory. No shared position lock, signal condition, webhook, or runtime activation.
DC20 has no ADR filter and no daily-close kill. ST4H skips **close hours** 0/4/8,
not candle open hours; both ATR interpretations were frozen before replay.

## Full-window closed trades

| Scanner | Trades | Win rate | Net PF | Gross USD | Net USD | Realized cash DD USD |
|---|---:|---:|---:|---:|---:|---:|
| ST4H Wilder ATR | 45 | 42.2% | 0.736 | -259.47 | -312.82 | 596.63 |
| ST4H SMA ATR | 42 | 45.2% | 0.754 | -203.05 | -252.81 | 467.29 |
| DC20 | 8 | 62.5% | 1.328 | +211.95 | +202.35 | 335.91 |

Fixed $1,000 entry notional **per position**, not a $1,000 shared account.
Fees 5.9bps each side; cash slippage $0.50 per BTC each side. Funding excluded.
No compounding, lot rounding, full risk gateway, spread history, or account margin
simulation. Drawdown above is realized closed-trade drawdown, not mark-to-market.
Close entries and exact stop fills across gaps are optimistic literal assumptions.

The quoted ST4H n41/PF1.23/+189 result is not reproduced. Both implementations
are negative even before costs. The quoted DC20 n5/PF1.65/+197 result is also
not reproduced, despite similar dollar profit. Eight closed trades cannot establish
a durable edge. Do not select an ATR interpretation after inspecting these results.
DC20's largest net winner is $294.38; removing it leaves -$92.03 across the
remaining seven trades (concentration diagnostic, not an alternate backtest).
Its 2025 realized closed-trade net is -$15.92, broadly consistent with "near zero."
Reproducing the original claims requires its exact data, dates, ATR initialization,
timestamp convention and trade ledger; these results do not identify which differed.

## Independently flat-started recent windows

| Scanner | Window | Trades | Net PF | Net USD |
|---|---|---:|---:|---:|
| ST4H Wilder | 90 days | 6 | 0.214 | -146.09 |
| ST4H SMA | 90 days | 5 | 0.550 | -48.61 |
| DC20 | 90 days | 1 | undefined (no loss) | +105.34 |
| ST4H Wilder | 14 days | 1 | 0 | -30.46 |
| ST4H SMA | 14 days | 2 | 0.794 | -5.26 |
| DC20 | 14 days | 0 | undefined | 0 |

These are independent starts, NOT slices of a continuous position book. In the
full replay DC20 ends short from Sep16; in the independent 90-day replay it ends
long from Aug22. Earlier occupancy changes which later signals are eligible.
Both ST variants end with an open long from Sep18 16:00 UTC. All final positions
remain open and are excluded from the closed-trade metrics; no forced terminal
exit was manufactured. No replay had an unresolved missing-bar position.

## Audit and tests

- `CONTRACT.md`: frozen assumptions and limitations.
- `st4h_attempt_01/`: 4h source receipts (URL, retrieval time, raw response hash,
  parsed response), bars, individual run decisions/trades and report hashes.
- `dc20_attempt_01/`: daily bars and separate run decisions/trades, report hashes.
  Original daily source receipt is retained in `../vela_family_v1/attempt_01/source/`.
- `tests/test_st4h_dc20.py`: prior-only ranges; equality; no ADR/kill;
  no entry-bar exits; stop-first ties; no exit-bar reentry; close-hour exclusion;
  Asia exits; ATR seed/cooldown/causality; gap quarantine; independent replay state.

Conclusion: ST4H fails this exploratory replay. DC20 is positive but sparse and
not promotion-grade. Neither is a demonstrated production edge. No deployment,
live trading, cron, registry change or capital permission change was made.
