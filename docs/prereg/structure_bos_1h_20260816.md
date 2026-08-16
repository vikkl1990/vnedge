# Pre-registration: `structure_bos_1h`

Status: **RESEARCH_ONLY**
Frozen: 2026-08-16
Timeframe: closed 1h canonical candles
Capital permission: none

## Hypothesis

A closed-hour break beyond causally confirmed higher-high/higher-low structure
(mirrored for shorts) may retain enough continuation over a multi-hour hold to
clear the perpetual-futures cost wall. This is a slow research candidate, not
a high-frequency scanner or an order permission source.

## Frozen rules

| Field | v1 value |
|---|---:|
| Swing detector | strict left=3, right=3 |
| Long structure | last high > prior high and last low > prior low |
| Long trigger | prior close <= buffered high and current close > buffered high |
| Short | exact mirror |
| Break confirmation | close crosses 5 bps beyond the last confirmed structure level |
| AVWAP veto | long rejects `strong_short`; short rejects `strong_long` |
| Stop | last opposite swing plus 10 bps, capped at 1.5× causal TR-ATR(14) |
| Fixed cost-edge hypothesis / target | 1.5R |
| Cost profile | `SWING`, taker entry and pessimistic taker exit |
| Expected hold / research time stop | 48 closed 1h bars |
| Minimum modeled net | 4 bps after fees, slip, and funding |
| Integrity rule | `data_quality == ok` plus exact quote/base volume |
| Higher timeframe | fully closed 4h bars, strict left=2/right=2 structure |
| Alignment | 4h UP + 1h BOS_UP, or 4h DOWN + 1h BOS_DOWN only |

The swing at bar `i` becomes visible only after bar `i + 3` closes. A known
quality break clears active structure so swings are never bridged across a
gap. Dual AVWAP uses exact `quote_volume / volume`; close or HLC3 proxies are
not permitted.

HH/HL classification and BoS/ChoCH event naming come from the pure
`vnedge.data.structure` measurement module. Equals are labeled `EH`/`EL` and
mixed structures (`HH+LL`, `LH+HL`) remain `range`; neither can produce an S1
continuation intent.

The 4h series is clipped at every 1h decision: a 4h candle is visible only
when its own `close_time <=` the current 1h close. Missing/degraded HTF data is
`BLOCKED`; 4h `RANGE/NONE` is `NEUTRAL`; opposing BoS or ChoCH is `CONFLICT`.
Alignment is only a filter and cannot bypass CostGate or registry eligibility.

## Decision path

```text
closed canonical 1h candles
  -> data-quality boundary
  -> confirmed 3/3 swings
  -> HH+HL or LH+LL
  -> 5 bps buffered one-time cross
  -> fully closed 4h confirmed 2/2 structure alignment
  -> opposite dual-AVWAP bias veto
  -> fixed 1.5R edge hypothesis
  -> SWING CostGate report
  -> virtual research intent only
```

The raw closed-candle API produces a deterministic ID and 48-hour time stop,
but its research intent is not an executable runtime type. The BaseStrategy
adapter only emits a virtual backtest intent after CostGate approval. The
strategy is not wired to a live lane and cannot add itself to
`CAPITAL_APPROVED`.

## Fixed acceptance and kill criteria

- Run walk-forward / embargoed out-of-sample evaluation on at least 90 days.
- Require at least 50 OOS trades for a verdict.
- Require positive median net bps after full modeled costs.
- Require drawdown to remain within the project halt policy.
- Any causality failure, non-positive OOS net, or insufficient 90-day sample
  kills the candidate. Promotion requires a separate human-reviewed registry
  change and shadow/paper ladder; it is never automatic.
