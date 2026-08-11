# PRE-REGISTRATION — funding_squeeze_continuation_v1

**Frozen: 2026-08-11.** No parameter, gate, or window change after this date
counts toward the verdict. Editing this file invalidates the judgment. Research
pre-registration, not a promotion or financial advice.

## 1. Identity
- Name: `funding_squeeze_continuation_v1` (already in the codebase, never judged)
- Decision timeframe: **1H**
- Markets (independent, never pooled): **BTC/USDT:USDT**, **ETH/USDT:USDT** (binanceusdm)
- Class: **offensive continuation.** Same feature as funding_mr (funding_pct
  extreme), **opposite action** — JOIN the crowd in a strong trend instead of
  fading it in chop. This is the direct test of "is the funding edge fade-only,
  or is there a continuation edge on the other side of the regime split?"

## 2. Frozen mechanics (strategy defaults — no tuning)
| Element | Frozen value |
|---|---|
| Entry LONG | `funding_pct ≥ 0.90` AND `regime_trend_up` (extreme positive funding + strong uptrend = shorts squeezed → join) |
| Entry SHORT | symmetric (extreme negative funding + strong downtrend) |
| funding_pct window | 240 bars |
| volume_z gate | `min_volume_z = 0.0` |
| Stop | `2.0 × ATR` |
| Target | `2.5 R` (2.5 × stop distance) |
| Regime | `RegimeParams()` defaults (ER + EMA alignment + ATR percentile) |
| Vertical barrier | `max_holding_bars = 48` (2 days on 1H) |
| Cost model | 5 bps taker + 2 bps slippage = **14 bps** round-trip |

## 3. Data
- Closed 1H binance candles + settled 8H funding (backward as-of merged, causal —
  the same construction funding_mr uses). No future data.

## 4. Windows (declared before any result)
- **Selection (seen):** 2023-01-01 → 2025-06-30, both markets.
- **Sealed tail (untouched):** 2025-07-01 → 2026-06-30 — opened once, only if §5 passes.

## 5. Selection criteria — OFFENSIVE_GATES (the system's own standard; ALL must pass)
- ≥ **15** completed trades (aggregate)
- Net expectancy **> 0**
- Profit factor ≥ **1.25**
- Payoff ratio (avg win / avg loss) ≥ **1.80**  ← offensive lanes win less often, bigger
- Win-concentration: largest single winner ≤ **40%** of gross profit
- ≥ **1** market individually net-positive

## 6. Untouched gate — SEALED tail (judged once)
- Net ≥ **+4 bps / trade**, PF ≥ **1.30**, payoff ≥ **1.8**, clean data. Verdict stands.

## 7. Explicit non-claims
- No L2/CVD, no ML. A PASS is a *candidate*, not a promotion — paper trading
  needs separate human approval; live capital needs the full pre-live checklist.

## 8. Burn registry
- 2026-08-11 · selection window declared seen on first run · tail sealed, unopened.
- 2026-08-11 · **§5 VERDICT: FAILED — but net-positive.** Seen 2023-01→2025-06,
  163 trades (BTC 69, ETH 94). Net **+$25.34** (BTC −$20.01, **ETH +$45.35**),
  **PF 1.05**, **payoff 1.67**, win-conc 3% (well-distributed, no lucky-trade
  dependence), win-rate 39%. Exits 95 stop / 41 TP / 27 max-hold. Passed
  trade-count, net>0, win-conc, ≥1-market; **FAILED PF (1.05<1.25) and payoff
  (1.67<1.80).** The least-dead result of the session — a *thin* continuation
  signal exists, but not enough to clear the offensive bar.
  **DISPOSITION: shelved. Tail stays UNOPENED. NO tuning-to-pass** — adjusting
  extreme_pct/gates on this seen result is the overfitting trap. Any refinement
  is a NEW pre-registration tested on untouched data (the sealed tail or fresh),
  never re-fit on this seen window. Symbol split is inverted vs funding_mr
  (ETH+/BTC− here; funding_mr is BTC-only) — noted, not actionable.
