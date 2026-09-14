# Market Stage Analyst and Crypto Fundamentals v1

Research-only addition to `/app/#analyst`, September 14, 2026. No scanner,
strategy ID, roster, capital, gateway or live parameter changes.

## Separate contracts and clocks

- `market_stage_analyst_v1`: canonical closed **4h and 1d** bars, each with its
  own as-of and hash. The existing 5m/15m/1h/4h alignment engine is unchanged.
- `crypto_fundamentals_v1`: public source observations with measurement periods
  and retrieval availability. Never retrospectively attached to a candle.
- Existing public funding, OI, spread and recent-trade samples remain positioning
  and liquidity. They are not fundamental earnings or institutional identity.

## Frozen stage definitions

This is a new descriptive hypothesis, not the poster's 30-week MA and not a
rewrite of HTF v2. Thresholds are preregistered in `market_stage.SPEC`; their
hash is carried on every result. Any change requires a new version and replay.

| Measurement | Definition |
| --- | --- |
| Input | Up to 512 contiguous closed, hashed, coverage-verified canonical bars |
| Warmup | At least 60 bars |
| Mean | EMA50, `adjust=False`, first-valid seed within the bounded suffix |
| Volatility unit | Arithmetic mean true range over 14 bars, not Wilder ATR |
| Slope | `(EMA50[t] - EMA50[t-5]) / ATR14[t]` |
| Displacement | `(close[t] - close[t-20]) / ATR14[t]` |
| Prior range | High/low of the preceding 20 bars, excluding the current bar |
| Advancing | Close > EMA50, slope > 0.15, displacement >= 2 |
| Declining | Close < EMA50, slope < -0.15, displacement <= -2 |
| Sideways | Absolute slope <= 0.15, absolute displacement <= 1, prior range width <= 6 ATR |
| Base after decline | Sideways and last confirmed directional stage was declining |
| Range after advance | Sideways and last confirmed directional stage was advancing |
| Unknown | No causal prior direction for sideways, or zero volatility |
| Transition | Other combinations |
| Stage confirmation | Two consecutive closes satisfying the proposed state; timestamp is the second close |

Confirmed 3-left/3-right pivots are supplemental measurements, available only
after the third right child's close. They do not secretly change the stage gate.
Relative-strength context is explicitly unbound in v1, not fabricated from an
unaligned benchmark. There is no forced 1→2→3→4 sequence.

The dossier shows current and previous stages, observed closes in state,
confirmation time, supporting measurements, conflicts, explicit breakout-watch
levels and invalidation levels. The breakout watch is a stronger condition than
the basic directional stage classifier; a trend can develop without a breakout.
Reference levels are fixed for the report, not stop orders. Every report includes
the definition hash and the complete input-window hash identity.

## Memory and provenance

State is reconstructed sequentially from the available contiguous suffix. A gap
resets memory. Its origin is exposed and **duration is left-censored**, not a
claim of lifetime time in state. The last 24 reconstructed changes appear in the
dossier. The worker additionally appends immutable `stage_report` records for
covered markets; these are historical observations, not journal ARM events.

A bad newest closed row cannot be replaced by an older good one. Official OHLC,
missing hashes, conflicting duplicate rows, identity mismatch and unverified
coverage are rejected. Forming/future rows do not contribute. Freshness is at
most 1.5 timeframe durations from the last close; stale classifications remain
historical only. Missing daily coverage does not block an independently valid
4h report, and it does not trigger backfill or an alternate candle writer.

## Fundamentals

Explicit initial identity mappings cover native BTC, ETH and SOL in USD/USDT
markets. BTC uses a proof-of-work network template; ETH/SOL use a smart-contract
network template. Unmapped assets require a verified asset/protocol mapping:
no fuzzy ticker search, stock-earnings proxy or default fundamental score.

The public collector reads DefiLlama chain-fee summaries with verified provider
chain identities. It records the latest completed UTC daily chart point, not a
forming daily estimate, for:

- User-paid fees: BTC, ETH, SOL.
- Net revenue after supply-side allocation: ETH, SOL.
- Token-holder revenue / fee-funded burns: ETH, SOL.

Every metric retains raw-response hash, source URL, unit, period start/end,
retrieval time, mapping contract hash and provider methodology. Revenue and its
attributions are **not additive**. Retained protocol revenue is not inferred by
subtracting independently timed series. The source distinguishes fees, revenue,
protocol revenue, holder revenue and incentives; definitions were checked against
[DefiLlama's definitions](https://docs.llama.fi/analysts/data-definitions).

Collection is bounded to seven requests per six-hour cycle, uses no credentials,
refuses redirects, caps response sizes and records failures. Freshness expires
three days after the measured period or receipt, whichever limit is reached
first. Zero is valid data; missing, invalid, stale and not-applicable are distinct.

Corporate earnings are not applicable to these native assets. BTC governance
vesting is not the same as mining issuance. Emissions, circulating supply,
usage, retained protocol revenue, unlock/event feeds and security/governance
events remain missing until separately verified source adapters exist. Missing
security data never means “safe” or “no exploit”. Overall fundamental health is
**not assessed**, even when fees are present.

Snapshots are first available at retrieval. A historical chart received today
does **not** make today's revised figures valid features for an earlier backtest.
`historical_backtest_eligible=false` remains explicit.

## Interface and evidence answers

The new **Stage & fundamentals** view is separate from technical alignment and
Public market conditions. It includes stage memory, confirmation/invalidation,
fundamental coverage and source references. Ask-the-evidence presets explain
stages and fundamentals without adding a paid model or trade authority.
Cached numeric values expire in the browser, including when updates stop.

## Validation and limits

Final local validation: **3,192 Python tests passed, 6 skipped**; **61 frontend
tests passed**; production frontend build passed. The 20 new backend context
tests cover this extension; 46 combined context/workspace tests also passed.
Live public collection produced seven observations (BTC fees; ETH and SOL fees,
net revenue and holder revenue), with no collection errors. Read-only dossier
and answer endpoints returned 200 and retained `can_trade=false` and
`can_promote=false`. Local canonical 4h/1d bars remain unavailable, so current
market-stage classifications honestly remain unknown rather than fabricated.
This release is local and uncommitted; no VM deployment was performed.

Regression tests cover opposite histories with the same range shape, prefix
causality, delayed confirmation, gap reset, stale and invalid proof, monthly
daily reads, native-token applicability, true zeros, source identity conflicts,
point-in-time availability, collection bounds and UI stale-value suppression.

This build does not establish trading edge. The future comparison must hold
the setup, entry/exit clock, fills, costs and holding rule fixed; use a separate
preregistered stage-context revision; reserve an untouched OOS window; report
net expectancy, drawdown, coverage, false breakouts and uncertainty. A daily
stage label is not permission to trade or to increase size.
