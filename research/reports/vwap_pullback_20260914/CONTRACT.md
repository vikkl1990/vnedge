# VWAP pullback bounce — bounded exhaustive research matrix

Frozen 2026-09-14 before computing outcomes. This is NOT the earlier
consolidation-breakout claim. No strategy registration or operational changes.

## Trial budget and evidence boundary

Exhaust every cell of this registered matrix, not arbitrary parameter space:
3 timing modes x 4 volume filters x 2 touch bands x 2 exit plans = 48 revisions
per symbol, 96 symbol/revision cells (BTCUSD and ETHUSD). All remain visible,
including zeros and failures. No additional thresholds after seeing results.

The same static Delta export `/tmp/vnedge-htf-recheck.PInWIi` contains
2026-09-01 to 2026-09-11 exclusive. This tape has already been used; it cannot
provide untouched OOS judgment. First seven/last three days are descriptive
blocks only. No protected historical judgment windows are accessed.

Use the previous VWAP experiment's validated stored 5m and 1m rows: canonical
source, content hash, closed proof, complete children and exact sums. Session
VWAP uses sum traded quote/base since 00:00 UTC. Any gap invalidates the rest
of the session; no candle fallback or fillna volumes. Long-only hypothesis.

## Common setup, known at closed recovery bar j

- Prior ATR20 = simple mean true range over 20 prior bars using 21 closes.
  Require those 21 bars and current bar eligible within this UTC session.
- Bars j-5,j-4,j-3 each close above their own exact session VWAP.
- Pullback bars j-2,j-1: last close below j-3 close, at least one red candle.
- Wide touch: at least one pullback low <= its VWAP + 0.50 ATR;
  neither low below its VWAP - 0.25 ATR. Tight touch: +0.25/-0.10 ATR.
- Recovery j: bullish body >= 50% of range, close in top 25%, close above
  prior pullback high and session VWAP. Session VWAP rising versus j-3.
- Reference entry not extended: actual adverse-adjusted next-open price
  must remain above the decision's last known VWAP and <=1 ATR above it.
- Stop frozen from origin setup: minimum low of j-2,j-1,j minus 0.1 ATR.
- Common opportunities are formed using the wide band; tight is a filter.
  After each common setup reserve 45 minutes from its close for EVERY cell,
  including cells that reject it. This covers delay15 + max hold30 without
  choosing opportunities from future realized outcomes.

## Timing modes

1. immediate: ARM at recovery close j, enter next 5m open.
2. delay3: timing control; ARM at j+3 close, enter its next 5m open.
3. higher_low3: same delayed ARM/entry as delay3, additionally require the
   latest two confirmed strict 3/3 lows at that time, with the second higher
   and anchored at j-2 or j-1. Use detect_swings unchanged, eligible bars only.
   Store both anchor and confirmation timestamps. Never backdate the entry.

Delayed modes require complete coverage through j+3 in the same session,
no stop touch on j+1..j+3, bullish permission still above VWAP. An unavailable
future decision is reported as censored, not simply omitted. Target is fixed
at actual ARM close + reward_multiple*(ARM close-stop). No target revision
after ARM. Original ATR remains fixed for all bands/entry extension checks.

## Volume filters (origin j only, not delayed hindsight)

- none;
- contraction: mean quote-notional of j-2,j-1 <= 0.75 * median of the ten
  preceding bars j-12..j-3;
- expansion: recovery notional >=1.5 * median of j-20..j-1;
- both conditions.

These are candle-volume measurements, not classified aggressive selling.

## Exit plans and cost

- short: 1.5R target, 15-minute time limit.
- standard: 2R target, 30-minute time limit.
- Reference stop/target must bracket actual adverse entry, otherwise reject.
- Validated 1m path: open gaps first, stop before target on ties, no favorable
  target-gap improvement. Missing path => censored. Timeout final minute close.
- delta_scalp_v2 config hash
  e04d112e35708157a77fc5b5721e4e2c4b2c0ce6d05c71b65bff3bf443e7b13a.
  Full taker 5.9bps incl GST on each actual simulated notional. Adverse
  execution 3bps/leg embedded in fill prices; nominal 17.8bps roundtrip.
  Stress at 6bps/leg on the same records. No fee waiver/maker or BBO-fill claim.
- Funding unknown: final net_bps=null. Report net_before_funding only.
  No CostGate wall subtraction and no sizing/portfolio/gateway simulation.

## Outputs, uncertainty and authority

Separate IDs `vwap_pullback_5m_<timing>_<volume>_<band>_<exit>_v1`, explicit
BTCUSD/ETHUSD universe and same immutable cost contract. Envelope at actual ARM;
origin episode, source hashes and pivot proof included. No registry mutation.

Keep every session/regime cell: four UTC six-hour blocks; local research ER20
>=.30 plus prior change direction = up/down, else range (not HTF-v2 regime).
Report sample counts, eligible/rejected/censored, gross expectancy,
before-funding expectancy/PF/unit-notional cumulative drawdown, stressed costs,
and first-seven/last-three blocks. Bootstrap whole UTC days, 2000 draws,
seed20260914; suppress intervals below 30 trades or five active days. Intervals
are descriptive, NOT multiplicity-adjusted. Repeated cells are correlated,
not independent trades; a positive best cell cannot establish an edge.

Compare delay3 vs immediate, and higher_low3 vs delay3 on matched episode
intersections, reporting excluded counts as well. Do not interpret a change
from selection as pure timing alpha. Fewer than30 trades => INSUFFICIENT.
No winner selection, adaptive scoring, promotion or deployment.
can_trade=false; can_promote=false; performance_eligible=false.
