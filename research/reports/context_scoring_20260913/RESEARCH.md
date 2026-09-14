# Research review: 22 indicator families, sessions and regime

September 13, 2026. Research only. No operational scanner modifications.

## Architectural conclusion

Use a setup-specific evidence score, not a count of agreeing indicators. The
score needs two distinct meanings: a transparent geometry score for diagnosis,
and a separately trained/calibrated estimate of post-cost outcome. A 60-point
geometry score is NOT a 60% win probability or a measured expected edge.

Group correlated inputs: structure, location, momentum, candle shape and
participation. Hard data-quality, identity, freshness, cost and risk gates stay
outside the score. A strong score must never compensate for a missing bar or
permission. Missing inputs remain visible; do not silently reweight a score.

Regime and time-of-day can condition a claim, but splitting a tiny sample into
many cells destroys its statistical support. No positive cell permission without
sufficient historical support; no fallback to an invented global success rate.

## Evidence consulted (not proof on Delta)

- Hansen, Kim and Kimbrough report recurring intraday volatility/volume patterns
  in BTC/ETH on other venues. This supports testing time-of-day conditioning,
  not assuming a profitable Delta session or hardcoding NY as superior.
  [Periodicity in Cryptocurrency Volatility and Liquidity](https://arxiv.org/abs/2109.12142).
- Osler empirically examined published support/resistance levels in FX and found
  predictive information for trend interruptions. This is mechanism motivation;
  it is neither a validation of our computed levels nor a crypto fee-aware result.
  [Federal Reserve Bank of New York study](https://www.newyorkfed.org/research/epr/00v06n2/0007osle.html).
- Bailey et al. explain selection bias from repeatedly testing strategies. A
  best score/indicator/window from this experiment cannot be promoted; prior
  research on the same ten days also counts as data reuse.
  [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).
- TradingView explicitly describes Heikin-Ashi prices as synthetic and Renko
  reconstruction from lower-TF data as an approximation. Features may use those
  transforms, but fills must remain on real timestamped market prices.
  [Non-standard chart data](https://www.tradingview.com/pine-script-docs/concepts/non-standard-charts-data/).
- A 2006 stock-return lunar-phase paper exists, but its setting is not Delta
  intraday execution. Only its bibliographic record/search excerpt was accessible
  in this review; full text was not verified. It does not justify a moon score.
  [ANU bibliographic record](https://openresearch-repository.anu.edu.au/items/d1c61773-d880-44c4-ba86-9180a38b4b8b).

No assertion that all academic literature on these22 families was exhaustively
reviewed. The priorities below are engineering/research judgments, not claims
that an entire indicator family is proven profitable or worthless.

## Coverage of every requested family

| # | Family | Treatment in this first test | Remaining issue / interpretation |
|---|---|---|---|
| 1 | Fibonacci retracement | .382–.618 band of latest ordered confirmed swing leg; location group | Not standalone Fibonacci edge; compare against preregistered non-Fibonacci bands later |
| 2 | Breakouts | Prior20 range fresh closed break; separate event family | Need follow-through larger than costs, not just a large breakout candle |
| 3 | Reversal | Prior20 wick poke and closed return; separate family | OHLC failed breakout, NOT a liquidity liquidation detector |
| 4 | Elliott Wave | Deferred | Freeze causal wave grammar, ambiguity resolution and confirmation before any run; no retrospective hand counts |
| 5 | Fair Value Gap | Explicit3-bar price nonoverlap, retest/expiry/invalidation | OHLC proxy, not proof of unfilled institutional orders; no imported SMC narrative |
| 6 | Candlesticks | Direction/close location and engulfing; one capped group | Only these patterns tested, not the entire candlestick catalog |
| 7 | Heikin-Ashi | Direction feature only | Actual prices used for entry/exit; no synthetic fill advantage |
| 8 | Moon phases | Deferred | Ten days is less than one lunar cycle; needs years, fixed ephemeris and placebo calendars before useful inference |
| 9 | Renko | Deferred | Freeze brick size, reversal/ordering rules and tick reconstruction; do not trade a retrospective chart brick |
| 10 | Harmonic patterns | Deferred | Freeze confirmed pivot sequence and ratio tolerances; many ratios multiply trials; no visual cherry-picking |
| 11 | Support/resistance | Prior20 boundary touch/reclaim | One location vote, not extra points for renaming the same level |
| 12 | Dynamic support/resistance | EMA20 touch/reclaim | Same adjust=False recurrence; not a second price source |
| 13 | Trend lines | Two confirmed same-kind pivots, fixed slope/touch tolerance | No moving anchors after outcome, no chart-pixel slope |
| 14 | Gann angles | Deferred here; existing separate study retained | Scale is a model parameter, not universal45°; prior test is not repeated or blended into this score |
| 15 | Momentum | MACD12/26/9 histogram sign | Different from HTF macd_impulse; never overwrites current permission |
| 16 | Oscillators | Wilder14 RSI and stochastic14 zones | One momentum group; correlated votes do not become independent evidence |
| 17 | Divergence | RSI at confirmed3/3 price anchors, once at second confirmation | Regular used for reversal, hidden for continuation-type candidates; no oscillator pivots or future swing knowledge |
| 18 | Volume | Exact quote-notional burst vs prior20 median | Unsigned participation, not buy pressure; no BBO/L2 information inferred |
| 19 | Supply/demand | Actual resting liquidity deferred | Range/FVG proxies do not establish the order book; L2 required for a depth claim |
| 20 | Market structure | Two confirmed HH+HL / LL+LH pairs | Structural state, not five extra independent indicators |
| 21 | BOS | Break of prior confirmed level aligned with prior pair trend | Separate episode, strict causality, not same-confirmation retrospective cross |
| 22 | CHOCH | Break against prior pair trend | Early reversal hypothesis, NOT proof that a new trend has completed |

16 families have limited explicit representations in this screen; six are
deferred. This is NOT a full backtest of all22 approaches.

## Scoring tested versus scoring eventually suitable for ML Lab

The first experiment intentionally tests a transparent common score, including
its weaknesses. Fixed trend/range weights are specified before outcomes. It
does not learn weights, calibrate probabilities or evaluate the deployed
MarketRegimeMachine. New hourly ER/EMA context is separately named.

A critical limitation: a continuation-aligned structure/momentum score can
structurally suppress reversal/CHOCH events. The resulting zero selections are
not evidence that reversal is intrinsically bad. They reject using one common
confluence score indiscriminately. Do NOT modify the frozen experiment after
seeing that; a family-specific score is a separate future version.

Proposed next research contracts (not implemented/promoted here):

1. Continuation: structure permission, level breakout/retest, participation and
   measured post-cost follow-through. Momentum is a potential filter, not a
   duplicated trigger. Compare minimal geometry versus each added group.
2. Range reversal: range location, failed break and closed reclaim; no requirement
   to already have continuation-direction HH+HL. Measure costs and adverse moves.
3. Transition/CHOCH: separate cohort until enough evidence exists. Do not award
   a trending permission just because the first countertrend break appeared.

Before learning weights: collect versioned candidate-time features and complete
reconciled outcome labels, including rejected-candidate counterfactuals kept
separate from executed-trade labels. Bind strategy, symbol, decision clock,
regime version, session policy, cost profile and feature fingerprint.

Train a bounded regularized model on development windows only; use event-time
purging/embargo and a separate calibration window. Benchmark against unscored and
simple grouped scores. Test incremental value of groups and time/regime
conditioning, not just AUC or train win rate. Compare net expectancy, exposure,
drawdown, coverage, turnover, payoff and confidence intervals. Count every tested
variant. Reserve a human-approved untouched judgment after exploration.

Any accepted score changes an entry set: new strategy revision, immutable
artifact hash and shadow-only promotion review. No automatic roster activation,
model update, sizing increase or live capital permission follows from this work.
