# Scanner signal gap — deployed evidence, 2026-09-17

## Finding

The deployed BTC/ETH continuation lanes are evaluating and rejecting, not
silently failing to place orders. They already support **both directions**.
In the sampled window neither emitted a signal or ARM. The current regime
does not permit this continuation family, and genuine missing trade coverage
also resets the hourly structure engine. More permissive gates would conceal
these problems, not establish an edge.

This audit made read-only VM inspections. Changes described below are local;
no deployment, capital approval, live enablement, or private execution client
was performed. FIRE_PATH.md remains the fire-path contract.

## Reproduction and scope

- VM repository: `/home/ubuntu/vnedge`, deployed commit
  `b260484e4daa6466536048f9f11840aef505f37b`.
- Runtime: `multi-lane-shadow`; roster `config/shadow-observers.v1.json`, v4.
- Journals under `logs/paper_trials/`, named
  `shadow_observe_htf_regime_continuation_15m_v2__btcusd_delta_india_btc_usd_usd_15m.journal.jsonl`
  and the corresponding ETH filename.
- Last 96 forward `lane_eval` records per pair at inspection:
  2026-09-16 01:00 UTC through 2026-09-17 01:30 UTC. Backfills and heartbeat
  records are excluded. This is not 96 uninterrupted clock slots.

| Evidence / failed gate | BTC | ETH |
| --- | ---: | ---: |
| Evaluations | 96 | 96 |
| Signals | 0 | 0 |
| Primary `regime_flat` | 96 | 96 |
| `family_mismatch` | 96 | 96 |
| `structure_not_ready` | 96 | 96 |
| `one_hour_structure_unresolved` | 96 | 96 |
| `side_alignment_missing` | 96 | 96 |
| `reclaim_not_meaningful` | 57 | 52 |
| `closed_15m_reclaim_missing` | 58 | 58 |

Failed checks overlap: they are not mutually exclusive causes. All 96 regime
explanations were `weekly_range_macd_off`. Structure explanations were
`structure_parent_ineligible` for 14 and `confirmed_swing_pair_not_ready` for
82 evaluations per pair. No ARM/order records were found in either inspected
journal. Thus this window does not implicate next-open confirmation, sizing,
the gateway, or the broker as the cause of zero signals: nothing reached them.

Latest context had 800 daily bars and EMA200 warm-up ready. Weekly range,
daily discount, and H4 down classified as the mean-reversion family, not
continuation. `regime_flat` is the existing scanner gate name; it does not
necessarily mean missing context or a literally flat price series.

## Data and recurring structure resets

Exact lake lookups found the same holes for both pairs:

| Missing 15m open, UTC | Invalid/missing 1m children, UTC |
| --- | --- |
| September 16 01:15 | 01:21, 01:22 |
| September 16 20:00 | 20:04 |
| September 17 00:30 | 00:42, 00:43 |

The recorder logged `delta websocket transport silent` at
2026-09-17 00:43:24.354 UTC. This is affirmative evidence of a public transport
interruption, not just speculation that the scanner's canonical wait is short.
The corresponding missing parents cause structure resets. The structure
algorithm intentionally discards confirmed swing history on invalid hourly
parents and needs fresh confirmed high/low pairs before becoming ready.

The read-only recovery planner at 2026-09-17 01:56:27 UTC examined the 48-hour
window September 15 01:00 through September 17 01:00. Each pair had 39 verified
hours and nine `RAW_DAY_PRESENT_COVERAGE_UNPROVEN` hours, with 25 invalid 1m
rows (`coverage_ok_unproven`). Both plans returned `GAPS_REMAIN`, zero rebuild
candidates, and `can_apply=false`. Raw-day presence is not proof of complete
trade coverage. Do not fill these holes with exchange OHLC or fabricated zeros.

Plan identities:

- BTC: `0782cc541f89584f7c80c1e0cf36d40b498df34bcab05e557c2649d106a73a16`
- ETH: `737f0c7b51f913d27f95c5c268d737763959cedbaa48ddd1fc9c5fa6d8a9f1ec`

The current HTF v2 contract explicitly permits `exchange_ohlcv_validated`
4h/1d context for denial-only permissions. These rows are not canonical
trade-backed decision bars; this audit does not relabel them as such. The
15m signal boundary still requires canonical evidence. The recent input
window contained 213 canonical rows and 11 gap-marked exchange placeholders;
the latter preserve time continuity, not permission to ARM.

## Local changes

1. HTF diagnostics now expose both-side policy, independent long/short regime
   permissions and setup readiness, and a distinct `gap_parent` failure.
2. Evaluation records have `evaluation_outcome` and `reject_category` while
   retaining every original gate. `SIGNAL` is not a claim of ARM or a fill.
3. Canonical bar/context timeout events retain their existing event kinds and
   now explicitly record `REJECT`, `no_bar` / `no_context`, and category.
   These are runtime prerequisite rejections, not fabricated scanner runs.
4. Hourly structure aggregation now respects explicit `is_closed=False` or
   `coverage_ok=False`/missing values in supplied flag columns. It also
   requires exact quarter-hour child timestamps. Previously these flags could
   be discarded while the derived parent was marked usable. Invalid parents
   remain gaps; this fix intentionally does not increase signal frequency.
5. Added a read-only journal audit command that deduplicates forward decision
   bars, counts every failed gate and exposes quality resets and identity.
   Whole-journal event counts are explicitly separated from last-N gate counts.
6. Regression coverage exercises both sides of the existing base, BTC, and ETH
   implementations; no opposite-side clone or threshold change was needed.

Run the report against a local journal copy:

```sh
.venv/bin/python -m vnedge.research.scanner_signal_gap --journal PATH --bars 96
```

## Registry and candidate disposition

Relevant registered identities (not an exhaustive registry dump):

| Scanner | Side | Decision TF | Clock | Status |
| --- | --- | --- | --- | --- |
| `htf_regime_continuation_15m_v2__BTCUSD` | Both | 15m | Next 15m open | Active shadow-observe; BTC Delta swing cost profile |
| `htf_regime_continuation_15m_v2__ETHUSD` | Both | 15m | Next 15m open | Active shadow-observe; ETH Delta swing cost profile |
| `htf_regime_continuation_15m_v2` | Both | 15m | Next 15m open | Registered shared implementation; not a third active pair lane |

Both pair lanes bind 4h/1d context. The existing
`liquidity_sweep_reversal_15m_v1` is research-only and not shadow-observe
approved. It is not the proposed 1h/4h prior-day/session sweep contract.
Do not activate it merely because it is registered or duplicate it under a
new name. Funding mean reversion remains KILLED. No additional scanner was
registered or promoted in this change; no after-cost edge was claimed.

## Remaining work and authority

Validation on the resulting local working tree:
`.venv/bin/python -m pytest -q` — **3,328 passed, 6 skipped** (191.47s).
This includes existing private-gap persistence, startup pre-client refusal,
venue-ID length, canonical-source and risk-gateway regression tests alongside
the new scanner/parent checks. The run reports dependency deprecation warnings;
no failures. These are local tests, not a production or profitability attestation.

- **Data:** historical gaps above are not safely repairable with the currently
  verified inputs. Restore reliable collection, or obtain independently
  verifiable missing trades and coverage before rebuilding parents. Do not
  suppress resets or extend timeouts as a substitute for coverage.
- **Hash lineage requirement remains open:** `bar_content_sha256` currently
  commits normalized bar content and source, not child hashes. Missing-child
  rejection exists, but it is not cryptographic parent-child chaining. This
  needs a versioned writer/reader/router/envelope migration and parity tests;
  silently changing the existing function would invalidate durable identities.
- **Paper:** active shadow-observe lanes cannot submit. A qualifying setup,
  clock confirmation, after-cost evidence, rounding, gateway approval,
  durable journal and separately authorized paper configuration are still
  needed. An optional candidate must first demonstrate measured after-cost
  behavior; this diagnosis is not that replay or authorization.
- **Live:** private truth/restart reconciliation, canonical-feed parity,
  production evidence and the full separate approval checklist remain
  prerequisites. Local collector work is not live authorization. This audit
  does not attest production readiness of that pre-existing work.
- **Observability:** this slice covers HTF evaluations and canonical timeouts;
  it is not a claim that every possible exception in every scanner now has
  complete rejection telemetry. The audit flags unexplained no-signal rows
  instead of inventing an explanation.

Live capital is unchanged and the allowlist is empty. Delta live startup
continues to refuse before constructing a venue trading client. Shadow cannot
submit; UI cannot fire; ARM remains size-free; full journal idempotency keys
remain on managed orders. The observed zero-signal causes are regime-family
denial plus structure readiness after real gaps—not a missing short scanner.
