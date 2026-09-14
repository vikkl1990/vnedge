# VWAP consolidation breakout — frozen exploratory ablation

Registered before first outcome calculation on 2026-09-14. No tuning or
best-variant selection. Existing strategy IDs and operational roster unchanged.

- Input: static `/tmp/vnedge-htf-recheck.PInWIi`, Delta BTCUSD/ETHUSD,
  2026-09-01 through 2026-09-11 exclusive. Already-seen exploratory data,
  NOT untouched OOS. Report first seven and final three days separately.
- Four new research IDs per explicit BTCUSD/ETHUSD universe:
  `vwap_consolidation_5m_base_v1`, `_vwap_v1`, `_volume_v1`, `_both_v1`.
  Long only; closed 5m ARM and next-5m-open simulated entry.
- Validate stored hashes/source/closed/coverage; require five eligible 1m
  children with matching OHLC and exact volume/notional sums for every 5m.
  All variants require uninterrupted eligible coverage from 00:00 UTC to
  decision close. A gap invalidates the remaining session, not filled forward.
- Exact session VWAP: Decimal sum(quote_volume)/sum(base_volume), reusing
  `vwap_from_sums`. This is the aggregate equivalent of SessionVWAP, NOT
  feeding synthetic trades or close-times-volume into its trade interface.
- Prior six bars form the consolidation. Width <= 1.5 times prior ATR20
  (simple mean true range, needs 21 prior valid bars). Trigger close > highest
  high of those six bars; trigger bar excluded from range and volume baseline.
- VWAP filter: all six prior closes within 0.5 prior ATR20 of their own
  session VWAP; current session VWAP > its value three bars earlier;
  trigger close above VWAP and <= 1 ATR above it.
- Volume filter: trigger notional >= 1.5 times median of 20 prior notionals.
- Stop = consolidation low - 0.1 prior ATR; target = trigger close +
  2*(trigger close-stop), frozen at ARM. Reject entry outside stop/target.
- One common opportunity universe, then filters. After each baseline ARM,
  reserve the next six bars for ALL variants (even censored/rejected events).
  This conservative episode de-duplication prevents overlapping variants from
  receiving different opportunities because of realized exits.
- Simulate up to 30 minutes using 1m OHLC: open-gap first, stop before target
  on intraminute ties, no favorable target-gap improvement, timeout at final
  minute close. Missing required path => censored, not flat/no-loss.
- Cost `delta_scalp_v2`, config hash
  `e04d112e35708157a77fc5b5721e4e2c4b2c0ce6d05c71b65bff3bf443e7b13a`.
  Taker fee 5bps *1.18 =5.9bps each leg, charged on each fill notional.
  Adverse execution 3bps per leg embedded in fill prices (combined estimated
  spread/impact; no separate spread double charge). Nominal roundtrip 17.8bps;
  actual cash-normalized charge varies with entry/exit price. No maker fills.
  Stress execution at 6bps per leg on SAME events, no alternate signal search.
- Settled funding absent => `net_bps=null`; report
  `net_before_funding_bps` only. No CostGate wall deducted from PnL and no
  order/gateway simulation claim. No sizing, leverage or portfolio DD claim.
- Predefined strata: decision-open UTC blocks 00–06/06–12/12–18/18–24;
  local research regime prior20 closes efficiency ratio >=.30 and signed
  price change => up/down; else range. This is NOT the deployed HTF telescope.
- Report per symbol/variant trade count, exclusions, raw and before-funding
  expectancy, PF, cumulative unit-notional drawdown in bps, daily clustered
  bootstrap 95% interval (2000 draws, seed 20260914), selected-minus-rejected
  expectancy difference with paired day resampling. Fewer than 30 trades
  means INSUFFICIENT; all subsets retained. No automatic winner/promotion.
- Immutable run artifacts: source/data/contract hashes, ARM envelopes, event
  details, counts and summary. can_trade=false, can_promote=false.
