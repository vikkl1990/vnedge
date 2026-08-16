# Finance Python / ML stack policy

VNEDGE uses libraries to improve measurement and falsification. No library can
replace CostGate, the registry, the mode ladder, reconciliation, or the risk
gateway.

## Adopted now

| Layer | Choice | Boundary |
|---|---|---|
| Canonical data | pandas, NumPy, PyArrow | Runtime path remains stable and deterministic |
| Parquet research | DuckDB and Polars optional extra | Read-only/offline research; never required for live startup |
| Statistics | NumPy and SciPy | After-cost Sharpe, PSR/DSR, PBO, CPCV, raw `N`, and correlation-adjusted `N_eff` |
| Rule research | Existing VNEDGE event-driven backtester | Next-open fills, explicit fees/slippage/funding, causal closed bars |
| Meta-labeling | sklearn `HistGradientBoostingClassifier` | Official take/skip candidate over rule intents; no standalone order path |
| Online monitoring | River optional extra | Delayed-label shadow probability and alert-only drift detection |
| AI text | Fixed-schema Pulse briefs | Observation language only; no trade verbs or routing |

## Deliberately deferred

- **vectorbt:** useful for independent research cross-checks, but not adopted as
  a second source of fill/cost truth. Add only with parity tests against the
  existing backtester.
- **statsmodels:** available in the optional research extra for HAC/autocorrelation
  diagnostics; it is not a runtime dependency.
- **LightGBM/XGBoost/PyTorch/foundation forecasts:** no validated target currently
  justifies the added hypothesis breadth.
- **River as a capital model:** deferred. `vnedge.ml.river_shadow` may update a
  versioned research shadow only after outcomes resolve; it cannot update live
  strategy permission, weights, sizing, or orders.
- **quantstats/empyrical:** reporting convenience only; current scorecards retain
  explicit after-cost definitions.

## Multiple-testing disclosure

Every grid or model-family report must retain:

1. raw configurations attempted (`N`);
2. aligned after-cost return series for each configuration;
3. correlation-adjusted effective trials (`N_eff`);
4. DSR calculated with its stated trial convention;
5. untouched OOS boundaries and embargo;
6. all killed/failed configurations, not only the winner.

`N_eff` supplements raw `N`; it never erases attempted hypotheses. Promotion
remains locked when the trial ledger or after-cost return matrix is missing.
