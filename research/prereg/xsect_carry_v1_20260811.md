# PRE-REGISTRATION — xsect_carry_v1 (cross-sectional funding carry)

**Frozen: 2026-08-11.** Construction below is locked from the exploratory SEEN
result; the sealed tail is opened once. Research pre-registration, not a
promotion or financial advice.

## 1. Identity
- Name: `xsect_carry_v1` — market-neutral cross-sectional funding carry
- Class: **relative-value, market-neutral** (not directional). The first
  non-single-asset edge candidate; harvests the funding risk premium the bot
  already validated (funding_mr), expressed across a universe.

## 2. Frozen construction (no tuning)
| Element | Frozen value |
|---|---|
| Universe | BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, LTC (binanceusdm perps) |
| Bar | daily (1d) |
| Factor score | trailing **3-day mean** daily funding per symbol |
| Book | long the **3 lowest**-funding, short the **3 highest**-funding, equal-weight, dollar-neutral (K=3) |
| Rebalance | **weekly** (every 7 days) |
| Carry income | shorts collect +funding, longs pay — included |
| Cost | **14 bps** on the fraction turned over each rebalance |
| Causality | score uses data ≤ day t; book held from t+1 |

## 3. Exploratory SEEN result (already computed, informs nothing further)
- 2023-01 → 2025-06: net Sharpe **+0.80**, t-stat +1.24, total **+82.0%**.
  Promising economic magnitude; t-stat sub-2, so **not proven** — hence this
  sealed-tail test.

## 4. Sealed tail (untouched) — judged once
- **2025-07-01 → 2026-06-30.**
- **OOS gate (all):** net Sharpe ≥ **0.5**, total return **> 0**, and the sign
  of the edge holds (carry stays net-positive after cost). Verdict stands.

## 5. Non-claims
- A PASS is a *research candidate*, not a promotion. Market-neutral execution,
  borrow/short feasibility, capacity, and per-symbol limits are separate
  pre-live questions. Live capital stays fully gated.

## 6. Burn registry
- 2026-08-11 · construction frozen from seen result · tail sealed, unopened.
- 2026-08-11 · **SEALED TAIL OPENED — VERDICT: FAILED.** 2025-07→2026-06:
  net Sharpe **+0.01**, t +0.01, return **−3.6%**, maxDD −23.3% (n=365).
  The seen +0.80/+82% was noise (t 1.24 was the tell); carry did NOT survive
  out-of-sample. Also: seen maxDD −40.9% on a nominally market-neutral book =
  it was carrying real regime risk, not a clean premium. **DISPOSITION: rejected.**
  Tail now spent for this construction. The discipline worked — this would have
  been a false promotion; the sealed tail caught it before any build.
