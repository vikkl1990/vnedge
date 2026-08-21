# Pre-registration — breakout_continuity_v1

Committed 2026-08-21, before any run under this ID. §7 is immutable for this ID.

## Hypothesis

In weekday expansion, after a confirmed range break and a shallow pullback that
holds, price continues in the break direction over the next hours to ~2 days,
with **gross edge > 0 before costs** on a sealed out-of-sample window.

This is a new signal class, not a gate on structure bounce. The bounce /
vol-band family is closed (`bounce_vol_band_20260821`, FAIL, sealed) and is not
reopened by anything here.

| Bounce (killed) | This hypothesis |
|---|---|
| Fades levels | Follows expansion |
| Needs levels to hold | Needs levels to fail, then hold as new support |
| Top-8% vol was toxic | Expansion is the habitat |
| High turnover | Fewer trades, longer hold |

## Windows — CORRECTED from the draft

The draft proposed a sealed window of 2026-06-01 → 2026-08-20. **That window is
burned.** A seven-configuration sweep of this strategy class ran on
2026-05-21 → 2026-08-19 on 2026-08-20 (commit `bfde9cd`, registry
`fa15bc6616dce830`), before any pre-registration existed: best PF 0.92, all
seven negative, gross/trade +8.28 bps against 11.55 bps of cost. The proposed
sealed window lies entirely inside it.

Locked instead, both untouched by any run in this programme:

| Phase | Window |
|---|---|
| Selection / development | **2023-06-01 → 2024-05-31** |
| Sealed OOS | **2024-06-01 → 2025-05-31** |
| Burned, unusable | 2026-05-21 → 2026-08-19 |

The 2026-08-19 cascade falls in neither window. That is a consequence of the
correction, not a choice to avoid stress — the sealed window contains its own
volatility history and is not screened for calm.

Also disclosed: 2025-05-21 → 2026-05-20 was burned for
`structure_bounce_prod_v1`, a different strategy_id. It is not used here.

## Scope freeze

| Item | Value |
|---|---|
| Strategy ID | `breakout_continuity_v1` |
| Symbols | BTCUSDT, ETHUSDT — evaluated SEPARATELY, no pooled PF |
| Venue | Binance-style USDT-M perps |
| Costs | canonical `CostModel`, realized (fees + slippage), NOT fee-only |
| Decision timeframe | 15m structure, 1h regime context |
| Protection | 1m for stops only — no 1m entries |
| Calendar | UTC. **Weekday only** (Mon–Fri); weekend = stand down |
| Session | prefer 12:00–16:00 UTC; outside allowed only if the expansion gate is true |

## Signal

**Regime (required):** weekday, and realized range (or BB width rank) ≥
**weekday-only p50** over a trailing 60-weekday causal window. Not pooled with
weekends: weekends run 0.54–0.58× the weekday median hourly range on 0.43–0.46×
the volume (measured 2026-08-21), so a pooled percentile is loosest exactly
where liquidity is worst.

**Break (15m):** close beyond the Donchian-20 extreme of the prior 20 closed
bars by ≥ 0.05 × ATR(14).

**Pullback:** price returns toward the broken level within 12 bars, no deeper
than the pre-declared invalidation.

**Trigger:** re-claim — a 15m close back in the break direction beyond the level.

**Exits:** stop 0.2 ATR beyond the pullback extreme; TP1 1R, TP2 2R; time stop
48h; invalidation on a 15m close deep through the level.

All features use closed bars only. No same-bar lookahead.

## Verdict rule — immutable

Evaluated in order. The first failure that applies decides.

| # | Test | Threshold | If it fails |
|---|---|---|---|
| 1 | **Gross edge per trade, before costs** | **> 0** | **KILL the family.** No cost, venue or fill change is attempted |
| 2 | n trades | ≥ 25 per symbol, or ≥ 40 combined if one is thin | INCONCLUSIVE — not promoted; sealed window may only be extended by a calendar rule declared now, never by cherry-pick |
| 3 | Net PF after canonical costs | > 1.0 | Not promoted. If gross > 0 but net ≤ 1.0, the finding is "real signal, insufficient after cost" and is recorded as such |

Test 1 is first deliberately. Structure bounce failed sealed at **gross/trade
−2.02 bps** — an absent signal, not a cost problem — and a cost-side rescue was
never available. The same test applies here before any cost discussion.

## Anti-rescue rules

- No parameter is retuned on the sealed window, for any reason.
- A negative sealed result is recorded and the family closed; thresholds are
  not widened, symbols not swapped, windows not extended.
- TOD jump thresholds, DOW return effects and vol-band gates are NOT entry
  filters here. The weekday + expansion regime filter above is the whole of it.
- No paper or live consideration until sealed gross > 0 AND net PF > 1.0.

## Sign-off

```
Pre-reg ID:     breakout_continuity_v1_20260821
Committed:      2026-08-21
Selection:      2023-06-01 -> 2024-05-31
Sealed OOS:     2024-06-01 -> 2025-05-31
Burned:         2026-05-21 -> 2026-08-19  (registry fa15bc6616dce830)
Cost profile:   CostModel.for_profile("delta_scalp"), realized
Verdict rule:   section above, immutable for this ID
```
