# Bot recovery: engineering acceptance, not permission to trade

Updated 2026-09-17. Capital allowlist, scanner parameters and live gates remain
unchanged. This work does not reactivate retired strategies or manufacture fills.

## Confirmed production findings

- Release `9534b81` exposes a consistent, read-only decision explanation in
  the journal projection, queue and cockpit. The two active HTF registrations
  are BTC/ETH variants of one continuation family, not two independent edges.
- In the 96 latest distinct forward evaluations per symbol examined before
  that deployment (ending 15:15 UTC), each had 92 primary `regime_flat` rejects
  and four `gap_parent` rejects. All 96 also failed structure readiness.
  The recorded regime reason was `weekly_range_macd_off`: mean-reversion
  context does not permit the continuation family. This is not an execution bug.
- At 15:51 UTC the canonical partitions contained 1,560 verified BTC 15m rows
  and 1,559 ETH rows; longest consecutive verified spans were 627 and 382.
  Canonical daily coverage was only seven BTC and six ETH bars (longest
  consecutive spans six and three). Counts are a dated inventory, not an SLA.
- Lane cache Parquets are OHLCV caches, not canonical evidence bundles.
  The deployed v2 contract separately permits validated-exchange HTF context;
  the new canonical-only offline diagnostic is intentionally stricter.
- Both active pair contracts lack a versioned OOS edge estimate. A qualifying
  setup alone cannot pass the strict CostGate. Target distance is not an edge
  estimate; a synthetic successful fill is not economic validation.

## Startup responsiveness repair

During deployment, full journal replay ran synchronously in OrderManager and
LivePaperSession construction on the HTTP event loop. Production requests timed
out while lanes were rebuilding. Existing lane journals exceeded 100 MB.

The lane builder now awaits one dedicated recovery worker for both constructors.
Only unpublished, lane-owned objects are initialized there; it does not start
feeds, run the strategy or submit orders. Execution returns to the main event
loop after construction completes. The single worker bounds recovery concurrency
and peak memory. Session initialization reuses its already-read records for
funding deduplication instead of parsing the whole journal a second time.

Full execution-history recovery remains intact; there is no tail-only shortcut.
Recovery exceptions prevent the lane from starting. A cancelled build cannot
start or submit through the discarded session. Tests check that the event loop
continues while recovery is blocked and that original order IDs, unresolved
orders, funding IDs and backfill evaluation keys survive.

This does not eliminate every startup delay: REST seeding, account restoration,
canonical context preparation and the next closed-bar evaluation remain separate
phases. HTTP responsiveness is not a substitute for decision readiness.

## Remaining acceptance sequence

1. **Continuity:** per-symbol connected/recording/coverage/readiness remain
   separate. Recover only independently verified missing trades. Never fill a
   gap by inventing candles or overwrite canonical provenance with venue OHLC.
2. **Replay input parity:** export immutable, source-labelled bundles following
   each registration's actual source policy. Validate hashes, coverage and as-of
   HTF selection. Do not stamp raw caches as verified evidence. Source-policy
   differences must be visible in reports.
   The offline tool now has an explicit `registered_context_v1` admission profile,
   registration fingerprint, causal boundary examples and read-only preflight.
   Decision bars still require canonical provenance. A subsequent local change
   captures the two frozen lanes' materialized startup inputs under
   `journal_dir/replay-inputs/<strategy>/<capture>/`. A manifest marks a complete
   capture and contains admission results. It preserves invalid rows, adds only
   exchange identity from the runtime lane if absent, and never manufactures
   hashes or coverage. The limit is 16 captures per registration, with explicit
   archival required when full; capture failure is logged, never a readiness
   claim. This is startup evidence, not every-decision or historical as-of proof.
   The VM raw caches still correctly fail admission. No historical round trip
   is claimed. Runtime capture deployment/verification remains pending.
3. **Research:** freeze separate continuation, range-reclaim and compression
   hypotheses before testing. Use already-exploratory data first; protect the
   pre-registered untouched windows. Compare costs, drawdown and coverage, not
   simply signal counts. No arbitrary indicator confidence points.
4. **Execution evidence:** obtain an actual supported edge model through the
   research protocol, then trace a genuine qualifying setup through clock,
   CostGate, sizing, gateway, journal, simulated fills, fees, exits and
   reconciliation. Keep synthetic mechanics fixtures separate.
5. **Promotion:** require the existing evidence/checklist and explicit human
   approval. No automatic capital allocation, risk-limit changes or live start.

Unfinished private-stream changes in the local worktree are a separate slice;
they are not included in the scanner/startup release.
