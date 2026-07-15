# Investing Algorithm Framework Adaptation Notes

Reviewed source: `coding-kitties/investing-algorithm-framework`.

## Useful Ideas Adopted

IAF's strongest idea for VNEDGE is the workflow separation: rank and filter the
universe before spending strategy or scanner CPU. VNEDGE now has a
research-only factor ranker that scores every configured exchange/symbol/timeframe
lane using closed-candle data:

- liquidity: recent average quote volume
- tradable range: ATR room over the fee wall
- trend quality: directional momentum weighted by efficiency ratio
- freshness: latest candle age
- coverage: available rows versus configured lookback

The output is written to `research/live_research/factor_ranker.json` and folded
into `research/live_research/latest.json` as `factor_ranker`.

## Safety Boundary

The ranker is not a strategy and not a promotion gate. Its payload carries:

- `research_only=true`
- `can_trade=false`
- `can_promote=false`
- `uses_only_closed_candles=true`

It answers one operational question: "which lanes deserve scanner attention
right now, and why are the others quiet?"

## Deferred Ideas

These IAF concepts are useful but should be built only after the scanner funnel
has enough evidence:

- SQLite-backed research result index for fast leaderboard queries across many
  bundles.
- Permutation testing for promoted candidates after the normal walk-forward
  gates, not instead of them.
- Parameter bundle archival for large exploratory sweeps.

## Ideas Not Adopted

VNEDGE should not adopt a generic live deployment wrapper from IAF. Execution
continues to run only through the existing VNEDGE gateway, mode ladder,
journal, reconciliation, and kill-switch invariants.
