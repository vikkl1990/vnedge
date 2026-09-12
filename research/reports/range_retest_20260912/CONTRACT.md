# Frozen replay — range_break_retest_5m_v1

2026-09-12, before inspecting this new strategy's historical outputs.

- One pass, unchanged source and SPEC, BTCUSD / ETHUSD Delta India separately.
- September 1 00:00 UTC through September 11 00:00 UTC exclusive. This ten-day
  window was already seen in other experiments: EXPLORATORY, not untouched OOS.
  No protected judgment window, tuning, favorable-subset selection or rerun.
- Input: original stored canonical 5m and 1m partitions from static export
  `/tmp/vnedge-htf-recheck.PInWIi`. Preserve OHLC/volume/source/content hashes.
  Attach lookup identity from the exchange/symbol/TF file path, not synthetic
  provenance. No official fallback, resampling, repair or filling of gaps.
- Evaluate each stored 5m decision in calendar order. All 22 consumed rows must
  pass the unchanged strategy's assertions. Missing decision rows counted as
  skipped. First 21 rows may be warmup; no extra pre-window history supplied.
- Record every evaluation, primary and all failed gates, scores, candidate
  envelope, episode/evidence IDs. Unexpected exceptions fail the run.
- Entry proxy is the next 5m open, coincident with decision close, read from
  the stored 1m open. This zero-reaction-time convention is optimistic about
  availability; the execution-friction model does not prove latency parity.
- At actual proxy entry, require price still outside the broken boundary;
  recheck stop < entry < target (mirrored for shorts),
  max extension 25% of range height, target room >= 2 x booked costs, and
  net reward/risk >= 1.5. Preserve original stop and target; reject a bad gap,
  never tighten stops or move targets. This is setup geometry, not CostGate
  approval, since no OOS edge estimate exists.
- One simulated position per symbol, deduplicated by episode ID. Overlapping
  candidates are counted, not separately booked. No leverage, sizing or kernel
  submission. Keep `can_trade`, `can_promote`, `performance_eligible` false.
- Walk eligible closed 1m bars from entry to entry+15min exclusive. Stop and
  target come from the actual signal. Adverse open beyond stop fills at the
  worse open; favorable open beyond target gets target, not improvement. Both
  touched in one minute -> stop first. Intraminute exits reserve through that
  minute's close (exact event time unknown). Otherwise exit at the close of the
  fifteenth minute: three completed 5m bars. No partial/trailing/maker fills.
- Validate path progressively: gaps before a known exit censor the outcome;
  missing bars after an observed exit do not erase it. Missing entry or censored
  path reserves the full 15-minute horizon, not an immediate new opportunity.
- Net bps = side-adjusted proxy move minus 17.8 modeled bps, `delta_scalp_v2`
  (11.8 fee/GST +6 friction). Cost config hash must match SPEC. No second
  spread/slippage deduction. Fee-only 11.8 and stress 23.8 sensitivities apply
  to the same entries, not fresh signals. Funding excluded; no promotion.
- Count fills, entry rejections, overlap, censoring, stop/target/timeout exits,
  gross/net means, PF, positive outcomes and descriptive five-day blocks.
  n<30 = INSUFFICIENT_SAMPLE, zero = UNMEASURED. No equity-return or live-fill
  claim. Freeze hashes of source, spec, harness, contract and inputs.

This tests the specified candle-based exit logic, not live execution approval,
account risk constraints, order-book liquidity or queue/latency reality.
