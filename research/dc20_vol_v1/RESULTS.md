# DC20 volatility filters: book changes, not demonstrated edge

Implemented and replayed five frozen modes independently, with three flat-start
windows each. BTCUSD official Delta India daily venue OHLC. Full window:
January 1 2025 through September 19 2026; November 2024 warmup. No missing bars.
June 2024 was not opened because it overlaps the repository's reserved BTC
judgment window. Therefore this is not a reproduction of the supplied Jun2024
through Sep2026 table. It is a controlled comparison against our preceding replay.

## Full-window closed trades

| Mode | Closed n | Win rate | Net PF | Net USD | Realized cash DD USD |
|---|---:|---:|---:|---:|---:|
| off | 8 | 62.5% | 1.328 | +202.35 | 335.91 |
| atr_exp | 3 | 66.7% | 2.406 | +186.89 | 132.92 |
| adr_band | 8 | 62.5% | 1.328 | +202.35 | 335.91 |
| skip_comp | 8 | 62.5% | 1.328 | +202.35 | 335.91 |
| atr_mid | 7 | 57.1% | 1.020 | +11.56 | 280.11 |

Dollar values use fixed $1000 entry notional per position, not compounding or
a fully simulated $1000 margin account. Fees 5.9bps per side and $0.50/BTC
slippage per side; funding excluded. ATR is SMA true range exactly as supplied,
not Wilder. Existing ST4H and deployed defaults remain unchanged.

## The open position changes the interpretation

Final daily close is $81,228.50 (Sep19 candle, closes Sep20 00:00 UTC).

| Mode | Still-open position | Gross unrealized USD | Closed net + open gross, less open entry fee/slippage USD |
|---|---|---:|---:|
| off / adr_band / skip_comp | Short Sep16 at 75,607 | -74.35 | +127.40 |
| atr_exp | Short Feb6 at 62,885.50 | -291.69 | -105.39 |
| atr_mid | Short Sep16 at 75,607 | -74.35 | -63.39 |

These are marked research P&L diagnostics, **not closed trades**, and omit funding,
future exit costs and liquidation/margin effects. No terminal exit was invented.
The ATR-expansion short has stop95,835 and target38,896.50; neither hit before
the boundary. Its occupancy prevented further entries. Closed-only PF2.406
therefore does not establish improved full-book performance. Removing its largest
closed winner also leaves -$6.80 across the remaining two closed trades.

## What actually changes

- Both ADR filters produce identical entries, exits and final inventory to off.
  ADR14/ADR50 at its nine entries (eight closed plus one open) ranges
  **0.78186–1.08076** on this tape, not the quoted0.82–1.39.
- ATR expansion has four entries, none shared with the nine baseline entry
  timestamps/sides. This is causal occupancy replay, NOT a filtered trade list.
  Entries: May22, Aug26, Nov15 2025 and Feb6 2026. Last remains open.
- ATR-mid has eight entries, five shared with baseline, three newly reachable.
- Off matches the saved preceding replay exactly for trades, metrics and final
  inventory in all three windows. This assertion is enforced by the runner.

## Recent independent starts

| Window | Modes | Closed n | Net USD |
|---|---|---:|---:|
| 90d | off / adr_band / skip_comp / atr_mid | 1 | +105.34 |
| 90d | atr_exp | 1 | +109.18 |
| 14d | all five | 0 | 0 |

Each recent replay starts flat with pre-window indicator warmup. These are not
subperiod slices of the continuously held full-window book. Every recent replay
ends with an open position; no-loss PF is undefined, not evidence of certainty.

## Code and safety checks

Research-only implementation in `src/vnedge/research/dc20_vol.py` and optional
volatility feature/gate support in `st4h_dc20.py`; default remains off. Unknown
modes rejected; ST4H refuses DC20 volatility gates. Missing/nonfinite/zero
volatility ratios fail the entry filter. Incomplete bar WATCH; gap while held
quarantines the book. Exits precede filters and remain stop-first with no kill,
trail or same-bar reentry. No runtime registry, live sizing or deployment changes.

`attempt_01/` contains the frozen contract, code/source hashes, all15 results,
per-bar ratios and actions, entry-identity diffs, trades and final inventory.
Tests cover threshold inclusivity, SMA true range versus ADR, causal warmup,
missing bars, zero denominators, WATCH, off-mode parity, occupancy-driven new
entries, exits while gate fails, and ST4H separation.

The "skip10% widest days" claim is untested: width definition and causal
percentile population/lookback were not specified. A full-sample percentile
would leak future information. No threshold search or automatic promotion ran.

**Conclusion:** ADR is a book-level no-op here. ATR expansion changes the book
but does not justify shipping a new default: three closed trades, an underwater
long-held short, no funding accounting, and no untouched validation.
