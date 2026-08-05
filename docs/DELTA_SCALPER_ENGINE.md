# VNEDGE Delta India Scalper Engine v1

## Status

Research-only. The engine has no broker, account client, order manager, or
submission method. A selected candidate can be converted to an `OrderIntent`
and evaluated by the existing `ScalperRiskGateway`; submission remains the
responsibility of VNEDGE's normal journaled execution path after promotion.

## Implemented flow

1. Delta public REST history seeds 1m, 5m, 15m, 1h, and 4h state.
2. Delta public WebSocket updates L2, trades, funding, ticker, and candles.
3. A candle is stored only after the next interval proves it closed.
4. The context builder computes the same deterministic features used by replay.
5. The regime engine classifies quiet, trending up/down, expanding, funding
   extreme, or unknown.
6. Momentum Burst and Imbalance Fade evaluate completed 1m/5m bars.
7. L2 imbalance and CVD are recorded as confirmation fields only. They cannot
   create, suppress, route, or promote a signal.
8. Candidates are costed with maker/taker fees, GST, optional DETO discount,
   slippage, opt-in Scalper Offer eligibility, and the applicable hold window.
9. Probability, confidence, and fee-adjusted expectancy gates rank the best
   candidate and journal the decision exactly once per scanner/market/side/bar.
10. The dashboard consumes the research snapshot. `can_trade` and
    `can_promote` remain false.

## Cost assumptions

Defaults are configuration, not immutable venue truth:

- Maker: 2 bps before GST.
- Taker: 5 bps before GST.
- GST: 18% of trading fees.
- DETO: optional 25% fee discount.
- Scalper Offer: optional and never assumed unless explicitly enabled.
- BTCUSD/ETHUSD eligible close window: 30 minutes.
- Other eligible futures: 15 minutes.
- Modeled slippage: 1.5 bps per leg by default.

Live fill accounting must replace these assumptions with Delta's reported
effective commission.

## Run the live research scanner

```bash
.venv/bin/python -m vnedge.runtime.delta_scalper_shadow
```

Only enable account-specific benefits when they are actually active:

```bash
.venv/bin/python -m vnedge.runtime.delta_scalper_shadow --scalper-opted-in --deto
```

Output:

- `research/live_research/delta_scalper_engine_latest.json`
- `logs/delta_scalper/delta_scalper_shadow.journal.jsonl`

## Run the full causal replay

```bash
.venv/bin/python -m vnedge.research.delta_scalper_backtest \
  --start 2025-01-01 \
  --symbols BTCUSD,ETHUSD \
  --scalper-opted-in
```

The replay uses next-1m-open entries, stop-first conservative resolution when
a stop and target appear in the same bar, full-path MFE/MAE, actual hold-time
fee eligibility, and no L2 because historical L2 cannot be reconstructed from
candles. It reports every day, week, month, and quarter, rolling expectancy,
market breakdown, false-signal rate, and 1x/5x/10x/25x/50x arithmetic scenarios
on $100 margin. Those leverage scenarios do not model liquidation and are not
an execution recommendation.

## Promotion gates

Paper trading remains locked until untouched results show all of:

- positive after-cost results in at least two markets;
- profit factor above 1.2 after costs;
- at least two positive months;
- no market or profitable month contributing more than 70%;
- complete-candle, no-repaint causal parity;
- Scalper window compliance.

Failure of any gate leaves the engine in research mode.
