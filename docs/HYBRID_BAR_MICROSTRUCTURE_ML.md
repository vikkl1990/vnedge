# Hybrid Bar + Microstructure ML

VNEDGE's first ML feature matrix is candle/bar based: OHLCV, ATR, trend,
funding, session, candle anatomy, and fee-wall context. That is the right base
for 5m/15m/1h scanner quality, but true scalping needs a second view of the
market: public trade flow and top-of-book pressure.

This PR adds an opt-in hybrid feature contract:

```text
closed candles + funding
        +
recorded tick/L2 events inside the same closed bar
        ->
HYBRID_FEATURE_COLUMNS
```

## Causal Rule

For candle row `i`, hybrid features use:

- the closed candle at row `i`
- book/trade events whose timestamps are inside that candle's interval
- no later bar, later trade, or later book snapshot

Future microstructure events must not change past rows. The tests mutate future
tick/L2 events and assert the selected past row remains identical.

## What Gets Added

The existing `FEATURE_COLUMNS` order is preserved. New models can opt into
`HYBRID_FEATURE_COLUMNS`, which appends:

- book event count and trade event count
- microstructure coverage flag
- mean and p95 spread in bps
- mean and last top-of-book imbalance
- last microprice displacement in bps
- mean top-of-book depth in USD
- total and signed trade notional
- taker buy ratio
- trade intensity per minute
- trade-price realized volatility in bps

## Where It Connects

`build_meta_label_dataset()` can now be called with `hybrid_params` and recorded
micro events. That lets the meta-labeler learn:

> Given this scanner fired, should the bot actually take it after seeing both
> bar context and local order-flow/tape conditions?

This is a filter/uplift layer, not a new direct trading path.

## Guardrails

- Existing saved bar-only models remain compatible.
- Missing tick/L2 data is visible via `micro_coverage = 0`.
- The default missing-data behavior keeps continuous micro fields as NaN so
  research cannot accidentally treat unknown order flow as neutral truth.
- No strategy is promoted, no lane is enabled, and no live behavior changes from
  this feature layer alone.
