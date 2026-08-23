# Pre-registration — ema_vwap_cross_v1

Committed 2026-08-23, before any run under this ID. The verdict rule is
immutable for this ID.

## Hypothesis

Price position relative to session VWAP, measured by a fast EMA crossing it,
carries directional information: after a cross the move continues far enough,
often enough, that **average gross bps per trade > 0 before costs** on a sealed
out-of-sample window.

The claim is different from the three closed families. They took their side
from where price sat against **structure it had built** — a level holding, a
level breaking, a moving average of price itself. This takes it from where
price sits against the **volume-weighted average other participants paid**.
VWAP is the only reference in this list that knows about size, not just price.

Provenance, disclosed: the mechanism was observed in a public strategy
catalogue on 2026-08-23. Every one of its twelve entries used EMA × VWAP with
an ATR trailing stop, and every one carried an unrealisable exit — median MFE
capture 93.9%. **Nothing about their reported performance is treated as
evidence here.** The mechanism is being tested from scratch because it is a
reference class we have not tried, not because their results were good.

## Symbols — and why not BTC/ETH

| Symbol | Burned windows | Decision |
|---|---|---|
| BTCUSDT | 5, spanning 2021-07 → 2026-08 | **excluded, exhausted** |
| ETHUSDT | 5, spanning 2021-07 → 2026-08 | **excluded, exhausted** |
| SOLUSDT | 1, 11 days (2026-07-08 → 07-19) | **included** |
| BNBUSDT | none | **included** |

This is a real constraint, recorded rather than worked around: after thirteen
burns, BTC/ETH have no clean multi-year window left for a trend-family
mechanism. Testing there would mean reusing data a prior decision has already
consumed. SOL and BNB are the honest venue for a new hypothesis.

The cost: both are thinner than BTC/ETH, so slippage and fill quality are
worse in reality than the model charges. A marginal PASS on SOL/BNB is weaker
evidence than the same PASS on BTC would have been, and that asymmetry is
accepted in advance rather than discovered later.

## Windows

| Phase | Window |
|---|---|
| Selection / development | **2023-01-01 → 2024-06-30** |
| Sealed OOS | **2024-07-01 → 2026-06-30** |

Neither overlaps SOL's only burn (2026-07-08 → 07-19). BNB has none. Data
begins 2020-09-14 (SOL) and 2020-02-10 (BNB), so both windows are covered.

## Frozen specification

| Item | Value |
|---|---|
| Strategy ID | `ema_vwap_cross_v1` |
| Decision timeframe | **1h closed bars only** |
| Side | EMA(9) crosses above rolling VWAP → long; crosses below → short |
| VWAP | rolling 24h volume-weighted average, causal, never session-anchored to a future boundary |
| Entry | at the close of the crossing bar |
| Stop | 2.0 × ATR(14) |
| Exits | 3.0 × ATR chandelier trail, opposite cross, 7-day time cap |
| Costs | canonical `CostModel`, realized, **plus signed funding per completed 8h** |
| Calendar | UTC, all days — no weekday filter, no session gate |

**The trail is 3.0 ATR, not 0.015 ATR.** The catalogue's tight trail is the
exact artifact this programme just measured: reproduced in our own engine it
gives median MFE capture 0.988 and a p90 of 2.264, banking more than the best
price the trade ever saw. A trail finer than the bar is not a parameter choice,
it is an unmeasurable one.

No gates. No regime filter, expansion band, stoch/OBV veto, session window or
confluence requirement. Every one of those was added to a previous family, and
none changed a gross edge that was not there. If the mechanism needs a gate to
show a signal, it does not have one.

## Verdict rule — immutable

| # | Test | Threshold | If it fails |
|---|---|---|---|
| 1 | **Gross bps per trade, before all costs** | **> 0** | **KILL.** No cost, venue, gate or fill change attempted |
| 2 | Trade count | ≥ 30 per symbol | INCONCLUSIVE — not promoted |
| 3 | **MFE capture, median** | **< 0.90** | Measurement rejected: the exit is finer than the data |
| 4 | Net PF after costs including funding | > 1.0 | Not promoted; recorded as "signal exists, insufficient after cost" |

Test 3 is new and sits before the profitability test on purpose. A book that
banks 90%+ of its maximum favourable excursion is not reporting a strategy, it
is reporting an exit the bar data cannot adjudicate — and no amount of edge
downstream of that number means anything.

## Anti-rescue rules

- No parameter retuned on the sealed window, for any reason.
- If sealed gross ≤ 0 the family closes. No successor differing only by EMA
  length, ATR multiple or timeframe.
- Gates are not added after a negative result to rescue it.

## Sign-off

```
Pre-reg ID:   ema_vwap_cross_v1_20260823
Committed:    2026-08-23
Symbols:      SOLUSDT, BNBUSDT   (BTC/ETH excluded — data exhausted)
Selection:    2023-01-01 -> 2024-06-30
Sealed OOS:   2024-07-01 -> 2026-06-30
Cost profile: CostModel.for_profile("delta_scalp"), realized, + signed 8h funding
Verdict rule: immutable for this ID
```
