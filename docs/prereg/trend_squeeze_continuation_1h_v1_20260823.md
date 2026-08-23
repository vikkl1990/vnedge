# Research registration: `trend_squeeze_continuation_1h_v1`

Declared 2026-08-23 for deterministic research replay. The implementation is
`RESEARCH_ONLY`, absent from `SHADOW_OBSERVE`, and never capital eligible.

## Frozen mechanism

- timeframe: closed 1h candles;
- Bollinger: 20 bars, 2.0 standard deviations;
- Keltner: EMA 20 with 1.5 ATR(20);
- compression: three completed bars with Bollinger inside Keltner;
- release: current closed bar leaves compression and closes outside its
  Bollinger envelope;
- trend: EMA20/EMA50 direction plus close on the trend side of EMA20;
- momentum: signed five-bar close change agrees with direction;
- participation: current volume is at least the prior 20-bar median;
- entry semantics: next bar open in deterministic replay;
- stop: 1.5 ATR;
- target: 2.5R;
- time stop: 12 closed 1h bars;
- cost family: conservative swing profile.

No intrabar trailing exit is used. Stop wins a stop/target tie. All data quality
and regime-route denials fail closed.

## First plumbing replay (already seen, not judgment)

The local canonical 2026-08-13 through 2026-08-22 slice was replayed only after
the rules above were implemented:

| Symbol | Signals | Gross | Net at gate cost |
|---|---:|---:|---:|
| BTCUSDT | 0 | 0 bps | 0 bps |
| ETHUSDT | 1 | +142.83 bps | +125.83 bps |

Combined: 1 trade, +142.83 bps gross and +125.83 bps after the conservative
CostGate assumption. This sample is far too small and symbol-dependent to be
evidence of an edge. It only verifies that the frozen mechanism can produce a
causal intent and complete the replay path.

This proves that the causal/runtime path can fire and resolve. One trade is no
evidence of expectancy. This slice is burned for promotion decisions. Shadow
permission requires a separately reviewed, longer chronological replay with
unchanged parameters and sufficient sample size.
