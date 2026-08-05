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

## CORRECTION (decay diagnostic) — chop, not death

Ran crypto_trend DOGE over the full contiguous arc 2024-01 -> 2026-08 (monthly
PnL + monthly efficiency-ratio regime). It is NOT dead: net +$108 over 2.5
years. It is violently regime-dependent — climbs to +$201 (by 2025-06), gives
back $137 (2025-08..11), recovers to +$161 (2026-02), gives back $94
(2026-03..05), recovers (2026-06/07 positive). The forward -$69 window caught a
bad phase. Winning months are more trending (ER 0.142 vs losing 0.130); the
auto "DEATH" verdict is a coarse-ER threshold artifact — the arc says
regime-gated. IMPLICATION: keep it, but it is a high-variance DIVERSIFIER (deep
68% strategy drawdowns make it dangerous solo), not a live anchor. funding_mr
BTC anchors; crypto_trend adds uncorrelated return, weighted down (inverse-vol
0.42). Earlier "decayed/edge gone" call retracted.
