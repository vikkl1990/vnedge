# Frozen experiment: QuantTrade full scanner and scoring replay

2026-09-12, before historical pipeline outputs. User requested the whole
scanner/scoring stack, not another independently rewritten detector screen.

## Identity and scope

- External repository `vikkl1990/VNEdge_QuantTrade`, commit
  `8085fc190aab07c6ebc9a7d5b921e9ab9f10a1ea`.
- Execute the actual `ScalpStrategy.analyze` and imported scanner, indicator,
  regime, scoring, EV and signal-builder code. Preserve tracked settings YAML,
  `paper_enforced`, thresholds, known bugs, routing and session rules.
- All 18 detector methods remain available as the upstream router permits.
  This is not a mandate to force disabled or ineligible scanners to fire.
- Fresh isolated checkout and empty learned state. No deployed weights,
  calibration artifacts, ML models, research overrides, or account history
  are provided. No fabricated substitute scores. Network calls refused;
  the upstream ML-unavailable path is recorded, not treated as trained inference.
- Replay time supplies datetime.now/time.time at each historical decision
  close, preserving session and cooldown rules. No outcome feedback is fed to
  weight/stop adaptation. Its empty-history behavior is retained. Thus this is
  a cold-state scanner/scoring test, not a replica of a trained production bot.

## Data and calls

- Static Delta export `/tmp/vnedge-htf-recheck.PInWIi`, September 1, 2026
  00:00 UTC through September 11, 2026 00:00 UTC exclusive. Already-seen,
  exploratory ten-day window, not untouched OOS.
- Verify stored canonical minute source, hash, closed proof and quality using
  the prior burst-response loader. No interpolation or fabricated bars.
- Build 5m/15m/1h only from complete eligible consecutive minute children;
  supplied 4h context is the explicitly official-OHLC cache, filtered by close
  time. It is not called trade-lake history. No official decision-bar fallback.
- Call on each eligible 5m close, BTC then ETH, matching upstream scheduling;
  symbol aliases BTC/USDT and ETH/USDT are upstream identifiers for these Delta
  inputs, not Binance data. Only rows closed at the decision are visible.
- Upstream data-store limit: latest 5,000 rows per timeframe. Preserve its
  unmodified treatment of gaps within supplied history; retain gap counts in
  evidence. Missing current minute/5m parent skips the call. Other context gaps
  are not silently repaired. This is not canonical production approval parity.
- One full pass only. No fix-and-retest, threshold search, learning-mode bypass,
  or forced synthetic setup. Stop/report runtime errors rather than hiding them.

## Measurements

- Count all analyze calls, primary final rejection reasons, detector calls/hits,
  swallowed scanner errors, final signals, BUY/SELL versus PRE signals,
  scanner attribution, confidence, metadata and ML availability.
- Wrappers only observe detector calls/results/errors and forward unchanged.
- Benchmark final BUY/SELL signals only: enter at minute open one minute after
  decision close, exit at minute open 15 minutes later; one position per symbol.
  Record overlapping signals but exclude them from the independent benchmark.
  Reserve each scheduled hold even when future data is missing; count censored
  outcomes. Require every minute in the decision-to-exit path eligible.
- Use `delta_scalp_v2` 17.8bps modeled taker costs (11.8 fee/GST +6 execution
  friction), plus predeclared fee-only 11.8 and stress 23.8 sensitivities.
- Preserve emitted stops/targets in evidence, but **do not claim the fixed
  15-minute markout replays upstream stops/partial exits/trailing or maker fills**.
  It is the same response benchmark as the prior screen, now after the complete
  upstream signal filter. No risk sizing, kernel, funding, or account PnL.
- Report every scanner's attrition and aggregate/per-symbol/per-scanner markouts.
  Below 30 measurements = insufficient; no signals = economics unmeasured.
  Positive mean alone is not edge proof. No post-hoc winning-subset promotion.

No live services, credentials, registry, roster or capital changes. All output
is `can_trade=false`, `can_promote=false`, `performance_eligible=false`.
