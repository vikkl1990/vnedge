# crypto_trend_atr_margin_v1 — cross-symbol generalization check (EXPLORATORY)

**Date:** 2026-08-14 · **Status:** exploratory, NOT a promotion judgment · **Verdict: does NOT generalize**

## Why this check
`crypto_trend_atr_margin_v1` entered the fleet because **DOGEUSDT 1h was the sole
survivor** of a ~100-symbol TradingView "Crypto Trend Indicator" batch
(`research/pine/top100_crypto_backtest.py`). Sole-survivor-of-100 is textbook
selection bias. Its live forward paper looked promising (+$16.15, healthy DD) but
is only **3 trades** — far too thin to trust. The disciplined question before
investing more in it: with the **same frozen params**, does the edge appear on
*other* liquid symbols, or was DOGE cherry-picked?

## Method
Frozen params (ema30/60, atr60, margin 0.30, stop 1.60), paper-like exits
(active-exit + 3× ATR chandelier trail), $500, default fees (5 bps taker) +
slippage. One config, no tuning. BTC & ETH 1h (the data on hand). Two windows.
Script: `scratchpad/crypto_trend_generalization.py`.

## Result

| Window | Sym | Trades | Net $ | WR % | PF | Max DD % |
|---|---|---:|---:|---:|---:|---:|
| Full 2023-01→2026-06 | BTC | 209 | **−87.73** | 28 | **0.85** | 26.5 |
| Full 2023-01→2026-06 | ETH | 332 | +88.27 | 31 | 1.09 | 20.2 |
| Recent 2025-01→2026-06 | BTC | 109 | **−124.43** | 26 | **0.56** | 26.6 |
| Recent 2025-01→2026-06 | ETH | 154 | +73.81 | 32 | 1.15 | 13.8 |

## Reading
- **BTC is a consistent, worsening loser** — PF 0.85 over 3.5 y, deteriorating to
  **PF 0.56** in the recent regime (−25% of the account). The strategy actively
  bleeds on BTC.
- **ETH is only marginally positive** (PF 1.09→1.15) with a **14–20% drawdown** —
  that fails the paper-trial 6% DD gate by 2–3× and the offensive PF≥1.25 gate.
  A 32% win rate makes it a high-variance, thin trend edge even where positive.
- **Conclusion:** the edge does **not** generalize. Neither liquid symbol passes
  any promotion gate. This strongly implies the DOGE result is **selection noise**,
  and the 3-trade forward paper cannot override it.

## Caveats (honesty)
- EXPLORATORY: BTC/ETH data has been seen for other strategies, so this is a
  clean *generalization* check (frozen params, no tuning) but not a pristine
  pre-registered judgment.
- Exit config (trail 3×, max_holding 48) is paper-like but may not match the DOGE
  paper lane exactly; a PF of 0.56 on 109 BTC trades will not flip to a robust
  edge on exit tweaks.
- DOGE 1h was not re-run here (only 1d local); the point is that BTC/ETH refute
  generalization regardless of DOGE's own multi-year curve.

## Implication
Combined with the funding_mr BTC paper trial **FAIL** (drawdown breach, net
negative, 8 trades), **neither current candidate is a robust edge.** Do not
promote either; do not tune crypto_trend on DOGE (that is the overfitting trap).
The real "grow the edge" work is a **new, generalizable signal source** — the
microstructure CVD/flow direction, or a multi-symbol-validated candidate that
clears gates on symbols it was *not* selected on. Any future crypto_trend
judgment must be pre-registered on untouched data and on symbols other than DOGE.
