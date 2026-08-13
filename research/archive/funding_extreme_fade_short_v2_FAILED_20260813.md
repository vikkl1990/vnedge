# ARCHIVED — funding_extreme_fade_short_v2 · FAILED sealed tail

**Verdict date:** 2026-08-13 · **Status:** FAILED → archived, not promotable.
**Pre-registration:** docs/prereg/funding_extreme_fade_short_v2_20260812.md (locked, unchanged).
**Runner:** research/funding_extreme_fade_short_v2.py · **Builder:** src/vnedge/plan/builders/funding_extreme_fade_short_v2.py

First dead-scanner rework under the Scanner Rework Protocol. Fair retrial under
the cost-aware TradePlan contract + plan_gate. It did **not** survive.

## Result (BTC 1h, Binance perps)

| Window | Trades | Net bps | Mean net bps | PF | Win % | Max DD | Worst |
|--------|:------:|:-------:|:------------:|:--:|:-----:|:------:|:-----:|
| Research 2023-01 → 2025-06 (diagnostics) | 44 | −1049.8 | −23.86 | 0.38 | 63.6% | 11.5% | −160 |
| **Sealed 2025-07 → 2026-06 (pass/fail)** | **29** | **−361.2** | **−12.45** | **0.72** | 51.7% | 8.4% | −144.8 |

**Sealed gate failures:** net_bps ≤ 0 · PF < 1.20 · mean_net_bps ≤ 0.
Trades = 29 ≥ 15, so this is a **real FAIL, not inconclusive** (§6).

## Why it failed (honest read)

The short fade has a *decent win rate* (52–64%) but **negative expectancy after
costs**: the ATR-1.5× stop is run over exactly when "crowded stays crowded" —
the continuation risk the pre-registration's own mechanism section named. The
regime filter (`not trend_up`) and the `close_z ≥ 2.0` stretch do not avoid the
large stop-outs; the small TP1 wins cannot pay for the occasional −144 to −160
bps losers. Conservatively, **no funding credit was claimed** — crediting the
short's funding income over the hold might narrow the loss, but that is a
*separate pre-registered hypothesis for a future round*, never a retrofit to
promote this seen result.

## Disposition (protocol §7)

- Builder marked inert (`SEALED_VERDICT = "FAILED 2026-08-13"`); not wired to any
  lane, not promotable. Kept as a scientific record + a working harness for the
  next locked funding-family variant.
- The funding-family edge that survives remains **funding_mr** (long+short,
  chop-gated) — this short-only extreme-fade variant is **not** an improvement.
- Any future funding-extreme retest must be a NEW locked pre-registration
  (e.g. crediting funding income, a wider stop, or a maker entry), evaluated on
  fresh untouched data — not this burned sealed window.
