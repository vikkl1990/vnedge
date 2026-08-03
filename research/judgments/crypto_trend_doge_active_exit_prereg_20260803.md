# Pre-registered judgment: crypto_trend DOGE under the real (active-exit) exit

**Declared 2026-08-03. Do not modify after declaration.** This freezes the
strategy, the exit configuration, the judgment window, and the pass criteria
BEFORE the untouched judgment data is run. The seen window (binanceusdm DOGE 1h,
**2025-07-04 → 2026-08-02**) is burned — it was used in Phase-1/Phase-2 exit
exploration and the in-sample sweep. It must NOT be used to judge.

## Origin

Phase 1 closed the research↔runtime exit gap: the backtester and paper/live now
run the SAME `ActiveExitState` machine, including a real per-bar ATR-chandelier
trail (#365, #366). A fixed-config OOS walk-forward on the SEEN window then
showed `crypto_trend` on DOGE flipping from REJECT (legacy exit, −$38.8) to PASS
(active-exit + trail 3×ATR, +$29.4, 44% profitable windows). That result is on
burned data, so it only justifies THIS pre-registered test — it is not a verdict.

## Frozen spec (do not change)

- **Strategy:** `crypto_trend_atr_margin_v1` with the deployed
  `CRYPTO_TREND_ATR_MARGIN_PARAMS` (unchanged). NO strategy-parameter tuning —
  the walk-forward grid is a single fixed entry `[{}]`.
- **Exit configuration (the thing under test):**
  `BacktestConfig(use_active_exit=True, trail_atr_mult=3.0, trail_atr_window=14)`.
  The trail multiplier `3.0` is a round number chosen a priori, NOT the best of a
  sweep.
- **Instrument:** `DOGE/USDT:USDT`, exchange `binanceusdm`, timeframe `1h`.
- **Method:** purged walk-forward, `train_bars=2880` (~4 mo), `test_bars=720`
  (~1 mo), `step_bars=720`. OOS = the concatenated test windows only.
- **Gates:** the standard `PromotionGates()` (min_splits 3, min_total_oos_trades
  10, min_profit_factor 1.1, max_window_drawdown_pct 15, min_is_retention 0.25,
  reject_zero_trade_windows True) via `evaluate_promotion`.

## Judgment window (untouched)

`binanceusdm` DOGE 1h, **2024-01-01 → 2025-07-03** (strictly BEFORE the burned
window's 2025-07-04 start). Downloaded fresh for this judgment; never previously
backtested with any exit configuration.

## Pass criteria (pre-registered — PASS only if this holds)

**`evaluate_promotion(walk_forward(...), PromotionGates())` returns `passed=True`
on the untouched window.** ONE run. The verdict — PASS or REJECT — stands and is
recorded in `burn_registry.jsonl`. A PASS makes crypto_trend DOGE (active-exit +
trail 3×) eligible for human-approved paper trading; it does NOT auto-promote or
touch live. A REJECT closes this hypothesis on this data.

## Honesty notes

- Legacy vs active-exit are BOTH run on the untouched window, but only the
  active-exit line is the pre-registered judgment; the legacy line is context.
- The judgment window predates the seen window, so it is genuinely out-of-sample
  for both the strategy exploration and the exit sweep.
