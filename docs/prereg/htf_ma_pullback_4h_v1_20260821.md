# Pre-registration — htf_ma_pullback_4h_v1

Committed 2026-08-21, before any run under this ID. The verdict rule is
immutable for this ID.

## Hypothesis

After a higher-timeframe trend is established on 4h bars, pullbacks that hold
against that trend resolve in the trend direction often enough that **average
gross bps per trade > 0 before costs**.

The economic claim is different from both closed families. Bounce and
breakout-continuity BOTH derived their side from a local 15m structure event —
a level holding, or a level breaking and being reclaimed. Both failed on gross
edge. Here the side comes from **persistent multi-day drift**, and the local
event only decides timing within a direction already fixed by the higher
timeframe. If drift exists at a horizon where 15m structure carried no
forward information, this is where it shows.

| Closed families | This hypothesis |
|---|---|
| Side from a 15m structure event | Side from 4h trend state |
| Many trades, tight stops | Few trades, wide stops in R |
| Cost is a large share of a small move | One cost event under a multi-day move |
| Funding immaterial at 1h holds | Funding is a first-class cost |

## Windows

Burned for trend-family work and therefore unusable: `crypto_trend_atr_margin_v1`
2024-01-01 → 2025-07-03, `trend_continuation_v1` 2024-07-10 → 2025-07-10,
`volatility_expansion_breakout_v1` and `funding_mean_reversion_v1`
2024-07-03 → 2025-07-03. Also burned: `breakout_continuity_v1` selection
2023-06-01 → 2024-05-31 and 2026-05-21 → 2026-08-19,
`structure_bounce_prod_v1` 2025-05-21 → 2026-05-20.

| Phase | Window |
|---|---|
| Selection / development | **2020-01-01 → 2021-06-30** |
| Sealed OOS | **2021-07-01 → 2023-05-31** |

Both predate every burn above. Disclosed deliberately: the sealed window spans
the Nov-2021 top, the 2022 bear market and the 2023 recovery, so it contains
sustained trends in BOTH directions plus an extended chop. The selection window
is mostly a bull advance. A trend follower that only works long will pass
selection and fail sealed, and that asymmetry is intended, not an accident of
date-picking.

Data begins 2019-09-08 (BTC) and 2019-11-27 (ETH), so both windows are fully
covered.

## Frozen specification

| Item | Value |
|---|---|
| Strategy ID | `htf_ma_pullback_4h_v1` |
| Decision timeframe | **4h closed bars only** |
| Side | EMA20 > EMA50 → long only; EMA20 < EMA50 → short only; otherwise flat |
| Entry | price pulls back to touch EMA20, then a 4h close back on the trend side |
| Stop | 2.0 × ATR(14) beyond the pullback extreme |
| Exit | EMA20/EMA50 cross against the position, OR 3.0 × ATR chandelier trail, OR 30-day time cap |
| Calendar | UTC. New entries on weekdays only; existing positions are managed through weekends |
| Symbols | BTCUSDT, ETHUSDT — reported SEPARATELY and combined |
| Costs | canonical `CostModel`, realized, **plus funding accrued every 8h of hold** |
| Position | one at a time per symbol, no pyramiding |

15m and 1m may be used for stop protection only. **The side is never taken from
a timeframe below 4h.** That is how the previous two families are re-entered by
accident.

## Verdict rule — immutable

| # | Test | Threshold | If it fails |
|---|---|---|---|
| 1 | **Gross bps per trade, before all costs** | **> 0** | **KILL.** No cost, venue, funding or fill change attempted |
| 2 | Trade count | ≥ 15 per symbol, or ≥ 30 combined | INCONCLUSIVE — not promoted |
| 3 | Net PF after costs INCLUDING funding | > 1.0 | Not promoted; recorded as "signal exists, insufficient after cost" |

Test 2's floor is lower than `breakout_continuity_v1`'s 25 because holds are
multi-day by construction. That is declared now, not after seeing counts.

Win rate is **not** a test. Trend systems earn through asymmetry and 35–50% is
normal; optimising it would be optimising the wrong quantity.

## Anti-rescue rules

- No parameter retuned on the sealed window, for any reason.
- Selection may reveal implementation defects and those may be fixed. It may
  **not** motivate a parameter change; that requires a new ID.
- Funding is not optional and is not removed to make net look better.
- If sealed gross ≤ 0, the family closes. No successor is written that differs
  only by MA length, ATR multiple or channel N — that is the grid-search death
  the 15m families already died of.

## Sign-off

```
Pre-reg ID:   htf_ma_pullback_4h_v1_20260821
Committed:    2026-08-21
Selection:    2020-01-01 -> 2021-06-30
Sealed OOS:   2021-07-01 -> 2023-05-31
Cost profile: CostModel.for_profile("delta_scalp"), realized, + 8h funding accrual
Verdict rule: immutable for this ID
```
