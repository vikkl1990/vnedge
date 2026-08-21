# Pre-registration — structure bounce under a volatility BAND

Declared 2026-08-21, before the sealed run. Nothing below may be changed after
the run. If the verdict is flat or negative, the band was sample-lucky and the
thresholds are NOT widened to rescue it.

## Hypothesis

A mean-reversion structure-bounce arm needs enough conditional volatility for
an after-cost residual to exist, and fails in the extreme tail where levels
stop holding. The tradeable region is therefore a BAND, not a floor.

This is a claim about strategy CLASS. It is not shared with breakout
continuity, which wants the opposite ceiling and needs its own
pre-registration.

## Frozen specification

| Item | Value |
|---|---|
| Strategy | `structure_bounce_prod_v1`, maker lane (`entry_mode=retest_limit`) |
| Arm | `StructureBounceArmSource(map_timeframe_mult=48, min_confidence=50)` |
| Vol floor | `min_bb_rank = 0.50` |
| Vol ceiling | top-8% expansion block RETAINED (`use_regime=True`) |
| Session gate | OFF — the band replaces it |
| Other gates | `use_stoch_obv=True`; remaining `ProductionGate` defaults |
| Trigger | `atr_stop_mult=2.5`, `stop_pct_floor=0.0055`, `stop_pct_cap=0.0095`, `vol_mult=0.0` |
| Exits | `SHARED_EXIT` — no-progress OFF, `absolute_max_bars=288`, ladder 1.5/2.5/4.0 |
| Costs | canonical `CostModel.for_profile("delta_scalp")`, realized (no safety buffer) |
| Symbols | BTCUSDT, ETHUSDT |
| Bars | 5m |
| Notional | $3,000 |

## Sealed window

**2025-05-21 → 2026-05-20 (365 days).**

Selection was performed on 2026-05-23 → 2026-08-21, which is now burned for
this decision. The sealed window ends before that period begins. A year is
used rather than 90 days because the selection sample was n=17-33, which the
prior round identified as the binding weakness.

`p50` and the 8% expansion threshold are structural quantiles computed
rolling WITHIN the run. They are not refitted on the sealed window and no
sweep is run there.

## What will be reported

n, PF, net bps, net USD, maxDD, gross/trade, cost/trade, win rate, and the
cost components. Same template as the selection sweep, so the two are
comparable.

## Verdict rule, declared in advance

| Outcome | Meaning |
|---|---|
| PF > 1.0 AND net > 0 AND n >= 40 | Band survives out of sample; eligible for a human-approved paper decision |
| PF > 1.0 but n < 40 | Direction consistent, sample still too thin; NOT eligible |
| PF <= 1.0 | Band was sample-lucky. Recorded, closed, thresholds not widened |

One run. The verdict stands whatever it is.

## Known limitations, stated before the result

- Selection ran on 90 days with n=17-33, and re-fetching the tape moved net by
  20-30%. That instability is not resolved by this run; a wider window only
  reduces it.
- The maker lane rests on a fill model the L2 replay has already contradicted:
  25% of resting limits fill passively, against the ~62% a bar-level touch
  test assumes. A PASS here is a statement about the VOL BAND, not a statement
  that the fills are real.
- The sealed window is a different volatility regime from the selection
  window. That is the point of sealing it, and it is also a reason a flat
  result would not be strong evidence against the mechanism.
