# Edge search — findings (2026-08-03)

Grounded crypto-native edge candidates run through the walk-forward gate
(BTC/ETH 1h, 2025-07-04→2026-08-02, `train 2880 / test 720 / step 720`,
standard `PromotionGates`). Exploratory candidate classes live in
`research/run_edge_search.py`, NOT the production strategy tree.

## Results

| candidate | symbol | PASS | trades | PF | net$ | exp/tr |
|---|---|---|---:|---:|---:|---:|
| incumbent funding_mr 0.85/1.5 | BTC | no* | 40 | 1.44 | +49.67 | +1.24 |
| session-gated funding_mr (08–20 UTC) | BTC | no* | 27 | 1.66 | +50.30 | +1.86 |
| **funding_carry_v1** | **BTC** | **YES** | 173 | 1.19 | **+82.38** | +0.48 |
| funding_carry_v1 | ETH | no | 175 | 1.06 | +27.84 | +0.16 |

\* `PASS=no` on funding_mr variants is the sparse zero-trade-window gate, not a
lack of edge.

## Read

- **session-gated funding_mr**: quality lift (PF 1.44→1.66, exp +50%) on 32%
  fewer trades — clears the fee wall more comfortably. CAVEAT: the 08–20 UTC
  window was chosen with knowledge of the Session Regime finding, so it is NOT a
  pristine a-priori test; needs an independent window to rule out soft-fitting.
- **funding_carry_v1 BTC**: first candidate to pass the full formal gate this
  round. Perp-native carry mechanic, distinct from funding_mr's reversion.
  CAVEATS: PF 1.19 is thin (fee/slippage-fragile), ETH does not confirm
  (PF 1.06), single window.

## Pre-registered NEXT step (do not modify)

Neither is promoted. Both are CANDIDATES for a second, independent
untouched-window judgment. Pre-register: **BTC + ETH 1h on the 2023-07 →
2024-07 slice** (needs a downloader `--until` backfill), same method, same
gates, one run. Promote only what passes there AND holds on both symbols or is
explicitly single-symbol. `funding_carry_v1` is the priority (it cleared the
formal gate); session-gating is a secondary confirmation. Cross-sectional
momentum (candidate 3) is deferred — it needs the multi-symbol portfolio
backtester (roadmap 10B).

## 2nd-window VERDICT (2026-08-03) — NO clean promotion

Backfilled BTC/ETH 1h + funding for 2022-09→2024-07 and ran the pre-registered
2nd-window judgment (research/validate_funding_carry_2ndwindow.py):

| candidate | window-2 | PASS | trades | PF | net$ | reject |
|---|---|---|---:|---:|---:|---|
| funding_carry_v1 | BTC | no | 277 | 1.28 | +187.77 | IS/OOS retention 19% < 25% |
| funding_carry_v1 | ETH | no | 306 | 1.08 | +67.53 | PF < 1.1 + retention |
| funding_mr_session | BTC | no | 26 | 1.10 | +7.75 | zero-trade windows |

**funding_carry BTC is OOS-positive on BOTH independent windows** (screen: PF
1.19 +$82; window-2: PF 1.28 +$188) — genuinely notable. But it FAILS the
IS/OOS-retention gate on window-2 (earns ~5x more in-sample), so the edge is
weak/unstable OOS, not robust. NOT promoted. ETH never confirms; session-gating
is too sparse.

CONCLUSION: the edge search (all 3 directions) found no candidate that cleanly
clears the full promotion bar on a 2nd untouched window. The two validated
earners (funding_mr BTC, crypto_trend DOGE) remain the only promotable edges.
funding_carry stays a documented near-miss (revisit if a robustness fix or more
data changes the retention picture); cross-sectional momentum remains the one
untested direction (needs the multi-symbol backtester).
