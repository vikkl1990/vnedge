# Frozen Gann-inspired slope-cross screen — 2026-09-13

Written before evaluating this rule on market data. One run, no parameter search.
The supplied poster does not specify an executable strategy. This is our explicit
normalised fan-line interpretation, NOT a replication of Gann's historical method
or of the unsupported `price * time = constant` equation. Square-of-nine,
additional angles, square breakouts and hand-picked origins are not tested.

## Fixed hypothesis

A volume-confirmed close through a slope projected from a confirmed price swing
predicts direction over the following 15 minutes, after a one-minute reaction
delay. Research ID: `gann_normalized_slope_cross_5m_v1` (not registered).

- Universe Delta India BTCUSD / ETHUSD; original stored canonical 5m bars.
- Window 2026-09-01 00:00 UTC inclusive to 2026-09-11 exclusive. Already-seen
  development data; NOT OOS. Do not consume any protected judgment window.
- Validate each stored decision with existing canonical assertion, original hash,
  closed/quality/coverage/source and positive finite base/quote volume. Retain
  calendar gaps as ineligible slots; reset anchors on a bad/missing bar.
- Swings: existing `detect_swings`, strict left=3/right=3, seven eligible closed
  bars. Anchor price is the pivot high/low, anchor time the pivot open. It only
  becomes usable AFTER its confirmation bar; no retrospective crossing at confirm.
- ATR20: arithmetic mean of 20 true ranges, requiring 21 consecutive eligible
  bars. At confirmation freeze `slope = ATR20 / 20` price units per 5m bar.
  This custom scale means one anchor ATR over 20 bars; it is NOT a universal 45°.
- Latest confirmed HIGH defines a descending line for LONG; latest LOW defines
  an ascending line for SHORT. Projection at close i uses `(i - pivot_index + 1)`
  elapsed 5m intervals from pivot open. No adjustment after confirmation.
- Anchor expires after 20 bars from confirmation, or on replacement/gap.
- LONG: previous close <= previous descending line, current close > current line,
  and current close > current open. SHORT mirrors these conditions.
- Volume: current quote notional >=1.5 times median of PRIOR 20 bars (not current).
- At most one event per anchor/side, keyed by symbol, anchor hash, confirmation
  hash and frozen spec hash. No chart-pixel coordinates or fitted scale.
- One preregistered descriptive ablation: volume + candle direction with the
  slope/anchor requirement removed. Same 21-good-bar prerequisite and timing.
  This is NOT a matched/randomised baseline, and samples can differ/overlap.
  Do not select it or tune either rule based on this comparison.

## Measurement, not an execution strategy

- Entry: 1m open one minute after decision close. Exit: 1m open 15 minutes after
  entry. No stop/target/trailing exits were specified in the poster; inventing
  profitable exits after seeing results is forbidden. This is fixed-horizon
  price-response backtesting, not complete strategy or live fill parity.
- One occupied interval per symbol PER arm (main/ablation separately); a censored
  event reserves its entire interval. Decision at exit time may begin a new event.
- Require every minute from decision close through exit open to be eligible;
  otherwise censor and report. Missing does not mean zero return.
- Model `delta_scalp_v2` config SHA
  `e04d112e35708157a77fc5b5721e4e2c4b2c0ce6d05c71b65bff3bf443e7b13a`:
  17.8 bps booked once (11.8 fee/GST + 6 friction); 11.8 and 23.8 sensitivities
  on identical events only. Funding excluded. No BBO/queue/account/risk execution.
- Report gross/net, trade count, win fraction, PF, both five-day descriptive
  blocks, rejection counts, all events and input/source fingerprints.
- Fewer than 30 measured per symbol = INSUFFICIENT_SAMPLE; otherwise nonpositive
  net mean = UNSUPPORTED_AT_MODELED_COST; positive = EXPLORATORY_LEAD_ONLY.
  No statistical significance or promotion follows from those labels.
- `can_trade=false`, `can_promote=false`, `performance_eligible=false` everywhere.
  No scanner/roster/VM changes. Store attempt_01 exclusively; refuse overwrite.
