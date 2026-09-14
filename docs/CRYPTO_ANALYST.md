# VNEDGE Crypto Analyst — architecture and end-to-end local build

## Build status · 2026-09-14

The second slice connects product discovery → public observations → immutable
evidence → canonical technical analysis → multi-timeframe investigation → local
evidence answers → saved reports and change events. It remains research-only.

Live public smoke test: **220 Delta perpetual products**, with all product pages
consumed, **220 ticker observations**, plus recent-trade samples for BTC/ETH.
No eligible canonical technical reports existed in the local candle directory.
Listing a product therefore does not create a technical score or setup.

Validation: **3,172 Python tests passed, 6 skipped**; **47 focused Analyst
tests passed**; **58 frontend tests passed**; production frontend build passed.
Browser checks covered discovery, source-cited public-condition answers,
empty canonical-history states and the narrow-screen layout. Cached public
observations expire locally even if polling stops; the server dossier cache
cannot extend their validity. Existing dependency deprecation and large-chart
bundle warnings remain non-blocking.

**Not claimed:** broad canonical candle history, predictive accuracy, settled
funding, proven profitable strategies, lane-consumed quote parity, an LLM model,
ML probabilities, automatic strategy activation, or deployment to the VM.

## Product promise

The next local extension adds a versioned **Market Stage Analyst** (4h/1d)
and a separate **Crypto Fundamentals** dossier, including causal stage memory
and source-recorded native-asset fee/revenue measurements. Definitions,
availability limits and tests are in [MARKET_STAGE_ANALYST.md](MARKET_STAGE_ANALYST.md).
The technical alignment formula remains unchanged.

A trader opens the product to understand the market, discover worthwhile
setups, inspect the opposing case, and maintain a focused watchlist. This is an
independent analyst, not an execution-lane diagnostic screen. Analysis can be
useful when no operational strategy has permission to enter.

Reference inspected: https://scanner.mrchartist.com/ (dashboard, scanner and
Sniper AI, September 2026). Adopt the overview → discovery → investigation
workflow, not its proprietary algorithms, institutional-activity assertions,
stock-market sessions or claims of high-probability setups. No assets copied.

## Target architecture

1. **Universe service**: versioned eligible perpetual products, contract specs,
   exchange and quote currency; distinguish discoverable from data-covered.
2. **Observation service**: immutable closed bars, separate BBO, funding, OI and
   classified trades. Each observation has event time, availability time,
   source, quality and expiration. No silent cross-venue substitution.
3. **Technical analysts**: structure, trend/momentum, participation, volatility,
   relative strength and execution conditions. Their output is typed evidence,
   not orders. Group correlated indicators; do not add five trend votes.
4. **Setup catalogue**: descriptive patterns such as compression, breakouts,
   VWAP recoveries and trend pullbacks. A pattern occurrence is not an approved
   strategy. Preserve definitions and both failed and passed conditions.
5. **Context assessment**: multidimensional direction, volatility, liquidity
   and UTC activity session. A future regime/session-specific weighting model
   needs its own frozen version and OOS comparison; no adaptive live tuning.
6. **Analyst dossier**: summary, support, contradiction, reference levels,
   conditional upside/downside scenarios, invalidation, evidence gaps.
   An optional LLM may narrate this typed evidence with citations; it cannot
   invent inputs, confidence probabilities, prices or executions. Reports
   retain input IDs, model/prompt versions, source timestamps and output hash.
7. **Workspace**: overview, opportunity radar, saved watchlists, symbol detail,
   multi-timeframe comparison, later alerts on changes rather than every bar.
8. **Evaluation**: capture immutable occurrences, label outcomes with explicit
   clock/cost/exit contracts, ablate features, validate OOS and calibrate
   probabilities separately. Analyst score is not ML probability or net edge.
9. **Execution bridge**: absent by default. Only a separately approved strategy
   may create an ARM envelope and use the normal risk/kernel path.

## Preserved first slice

`/app/#analyst` and authenticated/read-only `GET /api/crypto-analyst`.
Market overview, scan filters, both directions, searchable table, per-browser
watchlist, evidence-based brief, component breakdown, opposing evidence,
reference levels, conditional scenarios, source hashes and visible gaps.

The technical engine is deterministic and rule-generated, **not an LLM chatbot**.
The new opt-in worker below introduces public REST collection; the technical
engine still has no network calls. There is no model API request, inference fee,
execution adapter or roster change. Each venue is isolated; choices are 5m, 15m,
1h and 4h. Changing timeframe reruns that analysis, not a cross-TF merge.

Technical rankings use local canonical lake directories (maximum 24 symbols,
BTC/ETH shown explicitly when absent). The second slice additionally lists
discovered Delta products (maximum 500), outside technical rankings until
covered. A per-symbol dossier can inspect its canonical history on demand.
Breadth uses only current markets sharing the newest covered close, equal
weighted, with timestamp and denominator. BTC comparison binds the benchmark
window hash into the analysis ID as well as requiring exact close alignment.
Missing, stale and unavailable symbols do not become zero-return observations.

On-demand requests run off the event loop, with a single-flight 30-second cache.
UI polls every 30 seconds and marks reports older than 90 seconds as stale.
Reads are bounded to four recent daily/monthly partitions, 32 MiB per partition,
and twice the expected partition row count plus ten. No repair, backfill,
signing or writes to the lake.

### Analysis contract v1

- At most 512 closed bars as of the analysis time. A contiguous suffix of at
  least 60 valid bars is required. Old gaps truncate and are disclosed. A bad
  newest closed row does not fall back to an earlier good row.
- Check source, row hash, coverage, closed status, clock geometry and OHLC.
  Series scope comes from the exchange/symbol/TF directory; explicit row
  identities must match if present. Late/future/forming bars do not contribute.
- EMA20/50 are first-valid seeded `adjust=False` on this **bounded window**.
  This is intentionally a distinct analyst series, not committed HTF EMA state.
- Signed alignment: trend ±35, momentum ±25, prior-range position ±25,
  candle-direction volume participation ±15. Mixed inside ±20. Formula and
  constants live in `SPEC`; missing participation contributes no points and
  reduces disclosed coverage without reweighting remaining components.
- Momentum: 12-bar price change divided by 3 × mean true range14, clipped.
- Structure: close position within the prior20 range, scaled to [-1,1].
- Relative volume: current volume / preceding20 mean, current excluded.
- Compression: mean true range14 / preceding28 mean true range <0.75.
- VWAP recovery: current low ≤ session VWAP < close, bullish candle. This
  is a one-bar observation, **not** the multi-step bounce strategy contract.
- Session VWAP requires every bar from 00:00 UTC through the last closed bar,
  each with positive finite exact quote/base volume. Missing sums ⇒ unavailable.
- Relative BTC: difference in 12-bar percentage returns, same venue/timeframe
  and exact as-of close only. Percentage points, not a fitted beta residual.
- Activity labels use UTC 00–08 / 08–16 / 16–24 only as display context. They
  are not exchange opens, DST-adjusted sessions or scoring multipliers.
- Analysis ID binds spec hash, venue, symbol, timeframe and all contributing
  anchor hashes. It is not a decision_id and cannot be used as an order identity.

## New public evidence boundary

`analyst_public.py` uses only unauthenticated GET requests to the fixed India
endpoint. Allowed paths: products, tickers, and recent trades. Redirects are
refused, responses are bounded to 4 MB and requests time out after 15 seconds.
Products have at most five 100-row pages; repeated cursors fail visibly.
The default flow sample covers BTC/ETH only, at most eight explicitly requested
symbols. There is no historical OHLC download, second live candle builder or
write to the canonical lake.

- Products: live vanilla non-quanto USD perpetuals only. Contract base units,
  tick size, product ID and status are retained in a versioned product hash.
- Ticker: venue timestamp + receipt timestamp + immutable raw reference.
  OI uses explicit `oi_contracts`, not an ambiguous base-quantity field. Zero
  is preserved; absent fields are unavailable.
- Funding: the venue's indicative percent value, never settled cash or a new
  booked cost profile. No funding coverage receipt is minted.
- Book: crossed/locked books are rejected independently of OI. Contract sizes
  are converted with the bound product specification. REST best bid/ask is
  **not** the lane-consumed sequence, L2 queue evidence or quote-hold acceptance.
- Flow: aggressor classification comes from venue buyer-role / side fields,
  not candle colour. The last finite batch is an incomplete sample, not a
  full-time-window flow estimate; overlaps are not summed across polls. No
  institution identity or accumulation claim is inferred.
- Freshness: product discovery expires after two hours; ticker and flow after
  120 seconds, checked against both venue and receipt time. Future times fail.
  Expired observation values are hidden from current-condition UI metrics.
  Collector status becomes stale after 180 seconds. Failures remain visible.

Source contract: [Delta India public API documentation](https://docs.delta.exchange/),
products, tickers and public trades sections; actual payloads were smoke-tested.
Binance/Bybit retain their existing canonical analysis; this worker does not
claim newly collected derivatives or product coverage for those venues.

## Evidence, history and answers

`data/analyst/evidence.sqlite` is a separate compressed, append-only evidence
store. Only `analyst_worker.py` writes. Dashboard opens it read-only. Content
IDs bind kind, scope and body; reads verify the hash. Raw responses, normalized
conditions, product universes, reports, change events and worker status are
separate record kinds. Availability timestamps prevent reads from the future.
One advisory lease prevents two workers owning the same database.

At 1 GB the writer refuses further growth with a visible error; it does not
erase evidence. Operations must stop the worker and archive the database with
its WAL consistently before provisioning a new generation. Do not copy only
the main SQLite file while a writer is running. No automatic retention deletion.

Reports deduplicate by stable technical identity, not poll time. First capture
is a baseline, not a signal. Change events occur on bias/setup changes; a new
close with unchanged profile creates a report but no alert. History exposes the
last 30 reports/events; these are not fills or calibrated ML outcomes.

The dossier holds four independent closed-bar frames. Any directional
disagreement stays visible. Later REST observations do not become features of
an earlier scanner decision. The core v1 alignment weights remain frozen.

“Ask the evidence” supports trend, VWAP, reference levels, contradictions,
missing data, funding/OI/book and flow questions. It is local deterministic
retrieval, not a paid LLM or general-purpose chat. Answers carry exact source
IDs, input dossier ID and answer hash; unsupported data stays unavailable.
There are no tools for orders, promotion, training or roster mutation.
Queries are GET/read-only and may appear in server access logs: do not enter
personal/account information. Answers are snapshot responses, not streaming
recommendations. The server does not persist conversational input.

### API

All routes use existing dashboard authorization, including its explicitly
configured public-read-only policy, and `Cache-Control: no-store`:

| Route | Purpose |
| --- | --- |
| `GET /api/crypto-analyst?exchange=delta_india&timeframe=15m` | Discovery + technical radar |
| `GET /api/crypto-analyst/symbol/BTCUSD?exchange=delta_india` | Four-frame dossier |
| `GET /api/crypto-analyst/history/BTCUSD?exchange=delta_india` | Saved reports and changes |
| `GET /api/crypto-analyst/answer/BTCUSD?exchange=delta_india&question=why` | Local evidence answer |

Questions have a 500-character bound; symbols/venues are validated. All work
runs outside the HTTP event loop. There is no mutation route. Technical single
flight cache is 30 seconds; dossier cache is bounded to 32 markets / 10 seconds.

## Running locally / deployment handoff

One collection cycle, with no credentials:

```bash
.venv/bin/python -m vnedge.dashboard.analyst_worker --once
```

Continuous local evidence (60-second default), stopped with Ctrl-C:

```bash
.venv/bin/python -m vnedge.dashboard.analyst_worker
```

On the VM, after reviewing/committing/building this release, create the
`data/analyst` directory owned by the configured container UID/GID. Then:

```bash
docker compose --profile analyst up -d --build analyst-evidence
```

The dashboard image must also be rebuilt through the normal deployment
procedure to serve new API/UI code. The worker service mounts candles read-only,
only its evidence directory writable, runs with a read-only root filesystem,
no capabilities, no secrets and no privilege escalation. It has bounded memory,
log rotation, restart policy and an evidence-freshness/collection healthcheck.
Its health does not grant strategy readiness or trading permission.

## Remaining evidence / product work

1. Expand canonical trade recording through the existing sole owner, with
   per-market coverage proof and sufficient history. Product listing alone
   cannot repair missing history or create it retroactively.
2. Outcome datasets, context-weight comparisons, OOS tests and calibrated ML
   probabilities require independently frozen contracts and sufficient real
   labels. No manufactured labels or “VALID EDGE” badges.
3. Optional paid LLM narration requires the chosen provider, privacy/budget
   contract and an additional citation-verification boundary. The local answer
   mode remains usable without it.
4. External notifications are not enabled. Current change events are in-app
   only; destination and notification preferences require user configuration.

No deployment is implied by this local implementation. No proven edge or
profitability claim follows from a high descriptive alignment score.
