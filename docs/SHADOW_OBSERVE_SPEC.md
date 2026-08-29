# SHADOW_OBSERVE contract

`SHADOW_OBSERVE` is the live-public-data, virtual-outcome stage between sealed
research and capital evaluation. It is not a trading mode and grants no paper
or live permission.

## Permission boundaries

- `CAPITAL_APPROVED` remains the only strategy capital allowlist and is empty.
- `RESEARCH_ONLY` strategies cannot take capital.
- `SHADOW_OBSERVE` is a separate explicit allowlist. Frozen historical IDs
  remain replayable; the active roster uses `squeeze_expansion_breakout_v4`
  plus the historical quote-triggered `range_expansion_realtime_v1` and its
  pre-armed successor `range_expansion_realtime_v2`,
  `htf_structure_continuation_realtime_v1`, and the decision-time aligned
  `structure_bos_realtime_v2` and `session_continuation_realtime_v2`
  successors.
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
`squeeze_expansion_breakout_v3`/`v4`, it must be `5m`;
`range_expansion_observer_v3`/`v4`, `structure_bos_15m_trigger_v2`/`v3`,
their real-time successors, and both session-continuation revisions require
`15m`.

Research registration is not shadow permission. In particular,
`liquidity_sweep_reversal_15m_v1` remains replayable but is parked outside
`SHADOW_OBSERVE` after the current canonical BTC/ETH slice was gross-negative
on BTC and net-negative on both symbols. `trend_squeeze_continuation_1h_v1`
is likewise registered for deterministic replay only; it needs a longer,
chronologically separate evidence window before it may consume live shadow
resources.
V3/V4 arm from closed bars but
accepts only after three current top-of-book samples remain beyond the level
for at least five seconds. Failed probes re-arm per side; the opposite arm is
not burned. Its per-view quote queue retains a short bounded history sized beyond
the acceptance hold. Overflow evicts the oldest observation, increments an
explicit counter, and resets any in-flight probe because an uninterrupted hold
can no longer be proved. A slow lane therefore cannot backpressure the public
feed or silently manufacture acceptance. The optional plural
`MULTI_LANE_SHADOW_OBSERVE_SYMBOLS` creates one isolated lane per symbol. Bad
numeric values, missing fields, unknown IDs, killed IDs, and non-allowlisted
IDs fail startup.

The versioned roster supports multiple observer families and timeframes in one
single-writer process. `config/shadow-observers.v1.json` is the canonical
example: squeeze acceptance on 5m plus range expansion, HTF pullback
continuation, and session
continuation for BTC and ETH. The current roster uses quote acceptance for all
four families: closed bars establish causal levels and current, distinct BBO
events price the entry. Historical close-triggered IDs remain registered so
their evidence is reproducible. Lane IDs include strategy, venue, symbol, and
timeframe; duplicate IDs fail startup. Feeds remain shared only for identical
`(exchange, symbol, timeframe)` keys.

Quote-held acceptance uses exchange event time when the venue provides it and
stores local receipt time separately. Distinct exchange sequences count as
samples; duplicate, out-of-order, future-skewed, and over-lagged events fail
closed and do not advance the hold. Receive-time fallback is explicit for
sources without event timestamps. State transitions and quote provenance are
appended to the decision journal as `scanner_transition` records.

V4 squeeze does not inherit V3's close-times-volume VWAP proxy. It requires a
contiguous 288-bar canonical quote/base-volume window and makes the expansion
volume test binding before it arms the same quote-held acceptance engine.
The real-time range successor evaluates expansion context on causal 15-minute
closes but arms only an unbroken prior-range level. The active HTF continuation
scanner requires aligned closed 4h/1h structure, a meaningful 15m pullback and
EMA reclaim, then arms only the aligned side above/below the setup bar. Its
structure/ATR stop deliberately treats small adverse movement as noise; it has
no fixed profit cap and exits on hard protection, confirmed structure
deterioration, a late ATR trail, or the frozen time limit. It never flips from
one side to the other on a single dip. Session continuation arms its prior
four-bar range at the candle's close-time boundary and enforces the
12:00-16:00 UTC window again at quote/fill time. BoS uses one stable episode
per confirmed swing pair, so a rejected swing does not silently receive a new
probe budget every 15 minutes. A raw touch is insufficient: three distinct
quotes must remain through the level for three seconds, and excessive chase,
stale, duplicate, out-of-order, or post-session quotes fail closed.

Runtime economics and timeouts are frozen by exact strategy ID rather than
inferred from timeframe:

| Strategy | Cost family | Maximum virtual hold |
|----------|-------------|----------------------|
| `squeeze_expansion_breakout_v4` | scalp (`delta_scalp` on Delta) | 48 x 5m = 4h |
| `range_expansion_realtime_v1` | swing | 48 x 15m = 12h |
| `range_expansion_realtime_v2` | swing | 48 x 15m = 12h |
| `htf_structure_continuation_realtime_v1` | swing | 48 x 15m = 12h |
| `structure_bos_realtime_v2` | swing | 192 x 15m = 48h |
| `session_continuation_realtime_v2` | swing | 32 x 15m = 8h |

Startup refuses a registered scanner when its roster timeframe or hold differs
from this contract. Every closed-bar evaluation journals its primary failed
gate, all failed gates, distance-to-threshold values, exact thresholds, and
source provenance. Provenance includes the candle-source counts for the
decision window, whether exchange fallback participated, the latest canonical
timestamp, and a hash of the decision row. These fields explain a non-fire and
allow audited lake/live parity without changing eligibility or order authority.

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
