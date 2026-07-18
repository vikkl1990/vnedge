# Edge Model v1 Proof - 2026-07-18

Branch: `codex/edge-model-v1`  
Base dependency: `codex/edge-router-opportunity-labeler` / PR #189  
Runtime: VM Docker image `vnedge-research-loop` with `PYTHONPATH=/work/src`  
Deployment: not deployed

## What Was Built

`edge_model_v1` is a research-only learner over scanner opportunity rows.

Flow:

1. Generate raw scanner opportunity rows with `execution_edge_router_v1`.
2. Build a chronological feature/target table.
3. Train maker/taker net-bps regressors on the first 70% of rows.
4. Calibrate a selector on train rows only.
5. Apply the selector to the held-out 30% OOS tail.
6. Compare model-routed OOS performance against the raw all-scanner baseline.

Safety:

- `can_trade=false`
- `can_promote=false`
- no manifests written
- no runtime config changed
- no deployment performed
- OOS route decisions do not use forward truth

## Libraries Added

The PR adds an optional `quant-research` dependency bundle:

- `duckdb` for SQL over Parquet opportunity stores
- `polars` for fast lazy feature matrices
- `optuna` for bounded/pre-registered tuning
- `river` for future online shadow-learning
- `numba` for replay/MFE/MAE speedups
- `ruptures` for regime change detection
- `evidently` for drift monitoring
- `statsmodels`, `arch`, `skfolio` for statistical research, volatility, and
  portfolio allocation work

The first model implementation uses the existing sklearn dependency so the
production image does not require the optional research bundle.

## VM Proof Scope

Recorded candle data:

- Exchanges: `binanceusdm`, `bybit`, `delta_india`
- Symbols: BTC, ETH, SOL, BNB, XRP, DOGE
- Timeframe: `15m`
- Lookback: 30 days
- Horizon: 8 bars
- Opportunities: 10,534
- Train rows: 7,373
- OOS test rows: 3,161

Strict run:

- Min model trades: 20
- Min paper gate: avg net >= 25 bps and PF >= 1.5

| Metric | Raw Scanner Baseline | Edge Model v1 |
|---|---:|---:|
| OOS trades | 3,161 | 16 |
| Selection rate | 100.00% | 0.51% |
| Avg net bps | -13.3998 | -10.7997 |
| Profit factor | 0.6474 | 0.8127 |
| Win rate | 33.09% | 31.25% |
| Improvement | n/a | +2.6002 bps |

Strict verdict: `UNDER_SAMPLED`  
Reason: only 16 selected OOS model trades; need at least 20.

Diagnostic run with a 10-trade evidence floor:

- Verdict: `MODEL_IMPROVED`
- Same 16 OOS model trades
- Same +2.6002 bps improvement
- Still not a paper candidate because avg net and PF remain negative/low

## Honest Reading

This PR proves the bot can learn enough from opportunity data to avoid some
worse scanner contexts. It does **not** prove a profitable scalper.

The evidence improved:

- Raw scanners: `-13.3998 bps`
- Model-routed subset: `-10.7997 bps`
- Loss reduction: `+2.6002 bps`

But it remains non-tradeable:

- Avg model net is still negative.
- PF is still below 1.5.
- OOS selected sample is below the strict 20-trade floor.
- The train selector looked much stronger than OOS, which is a clear
  non-stationarity warning.

## Next Required Build

`edge_model_v1` should now be used as the proof harness, not as a trading
signal. The next improvement should target the data itself:

1. Persist opportunity feature rows to a compact Parquet/DuckDB store.
2. Add richer ex-ante features from prepared strategy columns, not just reason
   strings and route geometry.
3. Train per-regime/per-symbol models with walk-forward folds, not one global
   30-day split.
4. Add drift guards so a train selector that collapses OOS is automatically
   downgraded.
5. Promote nothing until model-routed OOS clears avg net >= 25 bps, PF >= 1.5,
   and minimum sample floors on fresh data.
