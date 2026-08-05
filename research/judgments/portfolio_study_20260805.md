# Portfolio study: combine the two earners (2026-08-05)

Built a portfolio backtester (src/vnedge/backtest/portfolio.py) and combined the
two live earners over the FORWARD window 2025-07-04 -> 2026-08 (after
crypto_trend's 2024-01->2025-07 judgment window). Deployed configs.

## Result

| standalone ($1k) | net | Sharpe | maxDD |
|---|---:|---:|---:|
| funding_mr BTC | +$52.28 | +1.43 | -3.86% |
| crypto_trend DOGE | -$69.13 | -1.36 | -10.33% |

- Correlation (daily PnL): **+0.05** — essentially uncorrelated (good for
  diversification IF both positive).
- Portfolio equal-weight: -$8.42 / Sharpe -0.26; inverse-vol: +$1.46 / +0.05.
  Diversification cut drawdown (5.2% vs 10.3%) and inverse-vol down-weighted the
  loser, but cannot turn a negative leg positive.

## CRITICAL FINDING

crypto_trend DOGE — promoted to paper this session on its 2024-01->2025-07 OOS
pass (+$191) — is **NEGATIVE on the forward 13 months** (-$69, Sharpe -1.36, 140
trades) with the exact deployed config + judged exit. The edge has decayed or hit
an unfavorable (ranging) regime for a trend strategy. funding_mr BTC still holds
(+$52, Sharpe 1.43). The "robust two-edge book" is currently a one-edge book.

## Actions

1. Investigate crypto_trend DOGE forward decay (rolling window: when did it turn?
   regime vs decay). It is live on paper — reconsider before any live capital.
2. Build the live book around funding_mr BTC (the edge that holds).
3. Portfolio machinery is built + tested — ready to combine 2+ genuinely-positive
   uncorrelated edges when we have them.
