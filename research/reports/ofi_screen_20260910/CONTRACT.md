# OFI measurement screen v1 — frozen before outcome calculation

Experiment, not a registered strategy or operational clock. Research only;
can_trade=false, can_promote=false. No kernel submissions, no roster changes.

Question: does signed, past-minute L1 order-flow imbalance predict subsequent
executable-direction returns on Delta BTC/ETH, after the repository's full
delta_scalp modeled costs?

Input: already-copied September 4, 2026 range-v2 lane BBO captures. This day
has already been inspected/replayed. This is exploratory temporal validation,
NOT untouched judgment. No canonical candle warmup is needed for this L1
measurement. No synthetic candle source or decision envelope is produced.

Method:

- Consume in captured receipt order; reject stale (>1s), negative-age,
  crossed/locked, missing/nonpositive size, overflow or non-advancing venue
  sequence rows. Sequence is numeric; duplicate samples cannot add flow.
- Compute standard best-level OFI changes: bid additions/improvements minus
  bid removals/worsening, minus ask additions/improvements plus ask removals/
  worsening. Normalize each completed receipt-time minute's sum by that
  minute's mean bid+ask queue size. Sizes remain contracts; the common
  contract multiplier cancels in this dimensionless measurement.
- Require >=10 valid updates, a first/last observation within 5s of the minute
  boundaries, no >5s internal/prior gap, and no invalid nonduplicate updates
  in the minute. No gap bridging. The 5s tolerance is a sampling rule, not a
  completeness proof.
- Calibration: 00:00–12:00 UTC only. Threshold per symbol = 90th percentile of
  absolute valid-minute normalized OFI. No return labels used to select it.
- Evaluation: minutes closing from 12:00 UTC onward. Direction = sign(OFI).
  Fire measurement only if abs(OFI) > frozen threshold. No reversal variant.
- Entry: first valid BBO received at least 250ms after minute close, within
  1s of that target. Exit: first valid BBO at/after entry receipt +60s or
  +300s, within 1s. One non-overlapping measurement per symbol/horizon.
- Reject paths containing an internal >5s observation gap. Require full exit
  horizon before end-of-day; do not silently mark to an earlier quote.
- Gross return uses ask-to-bid for long, bid-to-ask for short, normalized by
  entry price. This already crosses observed spread; subtract full configured
  delta_scalp fee/GST plus 3bps slippage on each side (17.8bps total today).
  Conservative sensitivity: also report fee/GST-only (11.8bps today), without
  promoting it to the default. Safety reserve is not booked PnL. Funding is
  unmodeled; no account-dependent close-fee waiver assumed.
- Report every symbol/horizon cell, sample count, gross/net mean, median net,
  win rate, PF, adverse inputs and rejection counts. No optimizer, best-cell
  selection, sizing, leverage, compounding or ML fit.

Limitation: snapshots can omit intermediate book events, so this is sampled
L1 OFI rather than a complete exchange event stream. One day's two symbols
and related horizons are correlated observations, not independent replications.
Any positive finding remains a hypothesis needing more days, frozen follow-up,
and execution validation. Never revive a killed engine from this screen.

Method reference: Cont, Kukanov & Stoikov,
[The Price Impact of Order Book Events](https://arxiv.org/abs/1011.6402).
Their equity-market contemporaneous impact result is not proof of predictive
profitability on Delta; the temporal separation above is the question tested.
