# SHADOW_OBSERVE contract

`SHADOW_OBSERVE` is the live-public-data, virtual-outcome stage between sealed
research and capital evaluation. It is not a trading mode and grants no paper
or live permission.

## Permission boundaries

- `CAPITAL_APPROVED` remains the only strategy capital allowlist and is empty.
- `RESEARCH_ONLY` strategies cannot take capital.
- `SHADOW_OBSERVE` is a separate explicit allowlist; it currently contains
  `structure_bos_1h`, `fee_wall_momentum_observer_v1`,
  `squeeze_expansion_breakout_v2`, `squeeze_expansion_breakout_v3`, and
  `range_expansion_observer_v1`.
- `KILLED` strategies cannot be observed or capital-enabled.
- The multi-lane process contains no live execution adapter. Its observe lane
  uses `RunnerMode.SHADOW`, `SimulatedExchange`, the risk gateway, the decision
  journal, and `ShadowOutcomeTracker`.

## Causal flow

```text
closed 1h bar
  -> structure_bos_1h causal signal and CostGate
  -> virtual-equity sizing
  -> PreTradeRiskGateway / KILL file
  -> journal shadow_intent
  -> ShadowOutcomeTracker
  -> later closed bars resolve stop / target / timeout
  -> journal shadow_outcome and dashboard virtual statistics
```

The structure strategy's frozen CostGate is a hard precondition to signal
emission. The gateway can still reject the sized intent. In SHADOW mode the
session returns before `OrderManager.submit`, so neither paper fills nor venue
calls occur. Intent keys are deterministic by strategy, symbol, decision bar,
and side. Stops win an intrabar stop/target tie.

The fee-wall observer is a distinct measurement experiment. It runs on closed
5-minute bars, records the first 13 bps/volatility-adjusted crossing across
5m, 1h, 4h, and 24h horizons, and attaches frozen virtual SL/TP levels. A
crossing is not treated as expected edge and cannot grant execution permission.
Same-direction horizons are episode-deduplicated, and later bars resolve the
virtual outcome through the same tracker.

## Runtime contract

The feature is off unless either the legacy singleton pair
`MULTI_LANE_SHADOW_OBSERVE_ENABLED=1` + an eligible
`MULTI_LANE_SHADOW_OBSERVE_STRATEGY` is supplied, or the preferred versioned
`MULTI_LANE_SHADOW_OBSERVE_ROSTER_PATH` points to a valid roster. The two
configuration contracts cannot be mixed. For
`structure_bos_1h`, the timeframe must be `1h`; for
`fee_wall_momentum_observer_v1`, `squeeze_expansion_breakout_v2`, and
`squeeze_expansion_breakout_v3`, it must be `5m`. V3 arms from closed bars but
accepts only after three current top-of-book samples remain beyond the level
for at least five seconds. Failed probes re-arm per side; the opposite arm is
not burned. Its per-view quote queue is bounded to the latest observation, so
a slow dashboard or lane cannot backpressure the public feed. The optional plural
`MULTI_LANE_SHADOW_OBSERVE_SYMBOLS` creates one isolated lane per symbol. Bad
numeric values, missing fields, unknown IDs, killed IDs, and non-allowlisted
IDs fail startup.

The versioned roster supports multiple observer families and timeframes in one
single-writer process. `config/shadow-observers.v1.json` is the canonical
example: squeeze acceptance on 5m plus range expansion and BoS on 1h for BTC
and ETH. Lane IDs include strategy, venue, symbol, and timeframe; duplicate
IDs fail startup. Feeds remain shared only for identical
`(exchange, symbol, timeframe)` keys.

Quote-held acceptance uses exchange event time when the venue provides it and
stores local receipt time separately. Distinct exchange sequences count as
samples; duplicate, out-of-order, future-skewed, and over-lagged events fail
closed and do not advance the hold. Receive-time fallback is explicit for
sources without event timestamps. State transitions and quote provenance are
appended to the decision journal as `scanner_transition` records.

`range_expansion_observer_v1` is a separate `1h` exploratory lane for the
first body-and-volume-confirmed close beyond a prior 12-hour range. It exists
because a continuation can begin hours after a squeeze arm expires; the
squeeze grace is not stretched retrospectively to catch such a move. The
already-seen 2026-08-19 window cannot be used to promote this observer.

The runtime snapshot publishes observe and paper counts independently. An
observe-only drill must show `orders_allowed=false` and
`live_orders_allowed=false`. Dashboard PnL is always labeled virtual; it is
never promotion evidence by itself.

## Non-goals

This feature does not change `CAPITAL_APPROVED`, revive funding mean reversion,
add a private stream, allow a scanner to submit orders, or auto-promote a green
virtual scorecard.
