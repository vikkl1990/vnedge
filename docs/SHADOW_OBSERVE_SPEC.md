# SHADOW_OBSERVE contract

`SHADOW_OBSERVE` is the live-public-data, virtual-outcome stage between sealed
research and capital evaluation. It is not a trading mode and grants no paper
or live permission.

## Permission boundaries

- `CAPITAL_APPROVED` remains the only strategy capital allowlist and is empty.
- `RESEARCH_ONLY` strategies cannot take capital.
- `SHADOW_OBSERVE` is a separate explicit allowlist; it currently contains
  `structure_bos_1h` and `fee_wall_momentum_observer_v1`.
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

The feature is off unless both `MULTI_LANE_SHADOW_OBSERVE_ENABLED=1` and an
eligible `MULTI_LANE_SHADOW_OBSERVE_STRATEGY` are supplied. For
`structure_bos_1h`, the timeframe must be `1h`; for
`fee_wall_momentum_observer_v1`, it must be `5m`. The optional plural
`MULTI_LANE_SHADOW_OBSERVE_SYMBOLS` creates one isolated lane per symbol. Bad
numeric values, missing fields, unknown IDs, killed IDs, and non-allowlisted
IDs fail startup.

The runtime snapshot publishes observe and paper counts independently. An
observe-only drill must show `orders_allowed=false` and
`live_orders_allowed=false`. Dashboard PnL is always labeled virtual; it is
never promotion evidence by itself.

## Non-goals

This feature does not change `CAPITAL_APPROVED`, revive funding mean reversion,
add a private stream, allow a scanner to submit orders, or auto-promote a green
virtual scorecard.
