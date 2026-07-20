# Pre-registered judgment: cross-venue liquidation-exhaustion maker fade

**Declared 2026-07-20. Do not modify after declaration.** This freezes the
hypothesis, the frozen parameters, and the pass criteria BEFORE the forward
data that will judge it exists. The seen 11-day window is burned (see
`burn_registry.jsonl`, kind `exploratory_burn`).

## Origin

Six-lens ("6 eyes") chart audit + backtesting converged on one lead with
fee-math on its side: fade the snap-back after a **liquidation flush**, entered
as a **maker** (the puking side fills your resting order), on the follower
venue. Delta publishes no liquidations, so the signal is **Binance** forced
orders and execution is on **Delta** — the surviving cross-venue echo shape.

## Frozen strategy spec (`liquidation_exhaustion_fade_v1`)

- **Signal (Binance `forceOrder` stream):** aggregate forced-order notional per
  1-minute bar by side. A *flush* = one-sided liquidation notional ≥ the
  **95th percentile** of that symbol's non-zero liquidation-notional bars
  (threshold fit on the TRAIN portion only, never on the judgment window).
- **Direction:** forced **sell** liquidations (longs liquidated → price flushed
  down) → **fade LONG**. Forced **buy** liquidations → **fade SHORT**.
- **Execution (Delta):** rest a **maker limit at the flush bar's extreme**
  (low for a long, high for a short); fill only if price trades to it within 3
  bars, else no trade.
- **Exit:** hold **30 minutes**, close at market — inside Delta's free-exit
  (<30 min) window.
- **Cost model:** ~5 bps round-trip (maker entry + slippage; Delta exit free
  <30 min). Judgment must use a **queue-aware fill on Delta's recorded L2
  book**, not the optimistic "fills if touched" proxy used in exploration.
- **Symbols:** Binance BTCUSDT/ETHUSDT/SOLUSDT liquidations → Delta
  BTCUSD/ETHUSD/SOLUSD execution.

## Pass criteria (pre-registered — a judgment PASSES only if ALL hold)

1. Aggregate OOS net **> +2.0 bps/trade** after the queue-aware Delta fill.
2. Sign-permutation **p < 0.05** on the judgment trades.
3. **Dose-response preserved:** pct95 net > pct90 net on the judgment window.
4. **≥ 40 days** of forward data (recorded strictly AFTER 2026-07-20) and
   **≥ 150** judgment trades pooled across the three symbols.
5. Positive on **≥ 2 of 3** symbols individually (not carried by one).

If any fails → REJECT and tombstone (no parameter tuning to rescue). One run;
the verdict stands. Recording is already live (`tick-recorder-delta`,
`liquidation-recorder`), so the clock started 2026-07-20; re-run when criterion
4 is met.

## Exploratory result (already-seen data, 2026-07-08→19 — NOT a verdict)

Binance-liq signal → **Delta real price** execution, maker fade, 30-min hold:

| | full-sample | OOS held-out (pct95) |
|---|---|---|
| net bps | +3.45 (pct95) / +5.18 (pct99) | **+3.16** (n=68) |
| perm_p | 0.23 | ~0.5 |
| dose-response | pct90 +0.4 → pct95 +3.5 → pct99 +5.2 | pct90 +2.0 → pct95 +3.2 |

Honest read: **positive, dose-responsive, and consistent across price source
(Binance +4.4 vs Delta +3.5 at pct95/30m) and across the OOS split at the
moderate threshold** — the strongest evidence produced in the research program.
BUT **not significant** (perm ~0.23), thin (11 days, one regime, 68 OOS
trades), and the pct99 extreme overfits (fails OOS). This is why it is
pre-registered for a forward verdict rather than promoted. No capital, no
promotion, until criteria 1–5 are met on untouched forward data.
