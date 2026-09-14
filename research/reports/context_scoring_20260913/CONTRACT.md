# Session/regime scoring exploratory screen — frozen before first run

Research ID family `context_score_5m_v1`; not registered or operational.
This is a new research interpretation of indicator families, NOT a modification
or a backtest of the deployed HTF-v2 scanner. All output is research-only.

## Inputs and clocks

- Delta BTCUSD and ETHUSD, September 1–11, 2026 (end exclusive), from the
  existing static `/tmp/vnedge-htf-recheck.PInWIi` export. Already-seen data.
- Use the stored 5m and 1m provenance validators from the frozen Gann and burst
  studies. No official decision fallback, filling gaps, or invented hashes.
- Features reset after every invalid calendar slot. Require 50 consecutive
  good 5m bars. Strict 3/3 price pivots: seven eligible bars, visible at the
  confirmation close; RSI sampled at pivot anchors, never backdated.
- Context: NEW research `hourly_er20_ema20_v1`, NOT MarketRegimeMachine.
  Only complete twelve-child hours. Last completed hour must equal
  floor(decision close, 1h)-1h. Twenty-one consecutive valid hourly closes;
  ER20 >= .30 plus close above/below EMA20 gives up/down, else range.
  Missing context is unknown, not range. No weekly/daily or fake HTF binding.
- Four nonoverlapping UTC six-hour blocks, assigned at decision close:
  00–06, 06–12, 12–18, 18–24. These are time blocks, NOT DST-aware NY/London
  exchange sessions. No best-hour search.

## Fixed event detectors

1. `breakout20`: fresh closed cross beyond PRIOR 20-bar high/low.
2. `wick_reversal20`: poke beyond that range then close back inside, candle
   body in reversal direction and closing in outer 40% of its own bar.
3. `ema20_reclaim`: prior close on opposite side of EMA20, current closed cross.
4. `bos3`: fresh close beyond last confirmed high/low, prior swing-pair trend
   aligned. A pivot confirmed on the decision bar cannot supply its break level.
5. `choch3`: same break against prior confirmed swing-pair trend.

One event per detector/bar; simultaneous opposite events are omitted. BOS/CHOCH
emit once per anchor/side. No automatic gate loosening, stops or target tuning.

## Indicator groups (geometry score, NOT probability)

Five groups, each capped at one vote regardless of its number of features:

- Structure: two confirmed HH+HL / LL+LH pairs aligned to event side.
- Location: range-boundary touch/reclaim within .25 ATR20; EMA20 touch/reclaim
  within .25 ATR; .382–.618 retracement of latest directionally ordered swing
  leg; latest three-bar FVG retest (12-bar expiry; invalidated on close through
  far edge); support/resistance trend line from two confirmed same-kind pivots
  with trend-consistent slope and .25 ATR touch tolerance. These are exact
  OHLC proxies, NOT evidence of resting supply/demand or order-book imbalance.
- Momentum: MACD12/26/9 hist sign; Wilder14 RSI 50–70 long / 30–50 short;
  stochastic14 %K 50–80 long / 20–50 short; confirmed RSI divergence (regular
  for wick reversal, hidden for other claims), only on second pivot confirmation.
- Candle: body direction + outer40% close, directional engulfing, Heikin-Ashi
  body direction. HA is FEATURE ONLY, never an execution price.
- Participation: quote notional >=1.5x PRIOR20 median (unsigned).

MACD/EMA pandas adjust=False, first-valid seed, min_periods=span. RSI SMA of
first14 changes then alpha1/14, both zero =>50. ATR20 arithmetic TR mean.
Unavailable features are null with explicit coverage. Group score is the mean
of available members; unavailable group => zero weight contribution (no
renormalizing across groups). This missingness policy is itself frozen.

## Primary comparisons, no tuning

- `baseline`: every valid detector event, regardless of score/context.
- `balanced60`: 20 points per group; threshold >=60.
- `regime60`: trend weights structure30/location20/momentum25/candle10/volume15;
  range weights10/35/15/25/15, threshold >=60. Refuse unknown regime and trend
  opposed to event side. These are preregistered heuristic weights, NOT learned.
- `session_regime60`: same plus training-only positive-net cell permission.
  Cell=(symbol, detector, UTCblock, hourly regime), >=30 measured training events
  across >=3 days AND mean modeled net >0. Otherwise abstain; no global fallback.
- Five diagnostic drop-one-group variants of balanced60, remaining four groups
  equally25 points. All five reported, never pick a winner after the run.

Fit cell permissions on Sep1–6 (exit strictly before Sep6); freeze, then evaluate
Sep6–11 (decision close >= Sep6+20min embargo). All comparisons use this same
split. Historical test is algorithmically held out here but ALREADY-SEEN research
data, NOT untouched OOS. No parameter search, no confidence/probability claims.
Minimum30 outcomes is a reporting floor, never statistical proof; report cell
support explicitly. Ten days cannot validate monthly/seasonal mechanisms.

## Outcome benchmark and limitations

Fixed-horizon response backtest: entry at canonical1m open one minute AFTER5m
decision close; exit15min later. No stop/TP, sizing, kernel or BBO fill claim.
Reuse frozen measurement engine; reserve each variant/family/symbol's occupied
interval even if future data censored. Other families are separate experiments,
not one aggregated executable portfolio. Same-event descriptive score attribution
uses baseline scheduled events; variant books independently reserve intervals.
Missing any minute from decision close through exit => censored, not zero return.

Cost profile delta_scalp_v2, config hash
e04d112e35708157a77fc5b5721e4e2c4b2c0ce6d05c71b65bff3bf443e7b13a:
17.8bps once, fee-only11.8 and stress23.8 sensitivity on IDENTICAL outcomes.
Safety wall is not deducted as PnL; funding excluded => no promotion evidence.
Report gross/net, PF, median, sequential cumulative-bps drawdown (NOT account
percent), count, daily-block descriptive uncertainty and all candidate exclusions.
No statistical significance claim after these multiple correlated comparisons.

Elliott, harmonics, Gann, moon phases, Renko and actual supply/demand are NOT
included as votes in this first screen. Their separate contracts/data needs are
listed in RESEARCH.md; don't claim all22 are fully implemented or tested.
Write attempt_01 exclusively; retain failures, hashes, events, trained cell table.
No VM, operational registry, roster, costs or capital changes.
