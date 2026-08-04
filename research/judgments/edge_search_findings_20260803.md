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
