# Pre-Registration — funding_extreme_fade_short_v2

**Candidate:** `funding_extreme_fade_short_v2`
**Date locked:** 2026-08-12
**Status:** ❌ **FAILED sealed tail 2026-08-13** — archived, not promotable.
See research/archive/funding_extreme_fade_short_v2_FAILED_20260813.md.
(Original locked spec below, unchanged.)
First dead-scanner **rework** under the Scanner Rework Protocol. A locked
funding-family hypothesis, not a revival of the old file as-is.

## 1. Why this one first
Same real premium as `funding_mr` (crowded longs pay funding); prior attribution
favored the short side on BTC; the old full-side version + optimistic costs may
have diluted results; runs cleanly on CostModel + TradePlan + plan_gate; one
side / one symbol / frozen params = minimal degrees of freedom.

## 2. Economic mechanism
When perpetual longs are crowded, funding prints rich positive; positioning is
extended; short-horizon mean reversion in price toward a recent mean has positive
expectancy **if** the market is not in a clean uptrend (where crowded can stay
crowded). Edge = funding/crowding premium + stretch fade; overextended longs pay.

## 3. Locked definition (frozen — no tuning after this)
Symbol BTCUSDT (Binance perps) only · Side **short only** · Decision TF `1h`.
- Funding extreme: `funding_pct >= 0.90` on rolling **240** bars
- Stretch: `close_z >= 2.0` on rolling **48** bars
- Regime: **not** `regime_trend_up`
- Mean: `SMA(close, 48)` must be **below** price at signal
- Entry: `next_open`
- Stop: **ATR 1.5×** converted to bps at the signal bar
- TP1 = 50% at `max(40 bps, 2.5 × round_trip_cost_bps)`; TP2 = 50% at distance to
  mean; if TP2 distance `< TP1` collapse to a single 100% TP at TP1; mean NaN → no plan
- Time stop: **24** decision-TF bars
- CostModel: taker **5+5**, slip **2+2**, safety **3**, funding accrue (conservative:
  no funding credit claimed in v2 primary)
- Max entry slip **15** bps; entry timeout **3** bars
- Hard gate (contract): `expected_net_bps <= 0` or TP1 `< 2× round_trip` → reject

## 4. Non-goals (forbidden during this test)
No long side; no other symbols; no changing windows (240/48/0.90/2.0); no
optimizing stop/TP after seeing results; no `limit_bps` until baseline `next_open`
fully reported; no ML filter in v2 primary.

## 5. Validation design (locked before run)
| Slice | Range | Use |
|-------|-------|-----|
| Research | 2023-01-01 → 2025-06-30 | diagnostics only (not pass/fail) |
| Sealed tail | 2025-07-01 → 2026-06-30 | **only** pass/fail |

**Success (all required on the sealed tail):** completed trades ≥ **15**; net
after CostModel `> 0`; profit factor ≥ **1.20**; max DD ≤ **15%** of the research
equity model; mean net_bps per trade `> 0`. **Fail if** any bar missed, the
implementation differs from this doc, or any param changed after the first sealed
look. Mandatory side-by-side vs classic `funding_mr` on the sealed window
(informative only, not a gate).

## 6. Power / honesty
Short-only + strict filters are sparser than full `funding_mr`. If sealed trades
`< 15`, the result is **inconclusive**, not a pass. Do not relax filters to hit 15.

## 7. Promotion path
sealed pass → candidate → human review → paper shadow lane (no capital conflict
with the current funding_mr paper) → paper GO → only then live_small eligibility.
Fail → `research/archive/funding_extreme_fade_short_v2_FAILED_<date>.md` + remove
from active builders.

## 8. One-line decision rule
Pass only if the locked short-only funding fade stays profitable after full costs
on the untouched sealed tail with ≥ 15 trades. Otherwise it stays dead — with
better evidence than before.
