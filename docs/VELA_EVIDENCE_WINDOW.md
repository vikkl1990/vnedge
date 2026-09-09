# Vela evidence window

Vela is a display cache, never the canonical lake or a decision authority.
This implementation changes no scanner, execution, cost, roster or capital rules.

## Candle proof

The dashboard reads persisted Parquet metadata without the legacy reader's
provenance defaults. Each DTO carries source, content hash, closed status,
quality, coverage, and independently checked identity/hash flags. Missing proof
is UNVERIFIED, not reconstructed. Official, repaired and unknown-source rows
are excluded from the Desk's canonical series; exclusion counts are visible.
A missing partition can be empty. An unreadable partition returns HTTP 503,
not a successful empty history. Conflicting duplicate opens also fail the read.

CLOSED and WATCH come from the DTO, not Vela's assumption that its last bar is
forming. STALE and ERROR describe display freshness, not operational readiness.
No new forming-bar producer or alternate public websocket is introduced.

## Cache and catch-up

Each pane owns its BarStore and feed. A poll returns every intervening bar in
open-time order; missing slots stay missing. An oversized catch-up reloads.
The provider keeps revision/timestamp bookkeeping, not another OHLC cache.

`series_revision` fingerprints the stored prefix through `revision_cutoff_ms`,
including proof metadata. Subsequent requests send `revision_before_ms`; the
server returns `previous_revision` for that same old prefix. Appends are normal.
Any change to the old prefix causes disposal and reconstruction of the pane's
cache, including repairs outside the visible range. Other panes are unaffected.
The frozen decision evidence is never updated to match a repair.

History completion distinguishes source exhaustion, requested depth and aborted
reads. Source exhaustion is not a statement that research coverage is complete.

## Markers and inspection

Journal events must contain a valid ARM DecisionEnvelope. The projection copies
its decision ID, snapshot, decision hash and explicit decision-bar open. Missing
or inconsistent evidence remains diagnostic-only. The UI does not invent IDs,
bucket event receipt times, or attach unknown-venue events to the active market.

An operational marker requires an exact CLOSED candle/hash match. Clicking its
bar opens decision/snapshot IDs, bound context refs and the current lake proof.
A revised or absent bar is reported as such and receives no operational arrow.
Studies/context decoration default off. Operator drawings remain display-only.

## Validation

- Python chart tests cover persisted proof, invalid metadata, source exclusion,
  repair versus append, HTTP errors and journal envelope validation.
- Provider tests cover missed-poll ordering, gaps, revision invalidation,
  truncated catch-up, errors, disposal and source/market isolation.
- Marker tests cover explicit bar identity, missing evidence and repaired bars.

This display work does not fill lake gaps, prove BBO approval parity, or unlock
Arena backtests that lack their required canonical history.

## Deployment checks still required

Run a browser smoke test against the deployed API, including a market switch,
failed history request and controlled revision fixture. Profile API latency on
the VM's largest partitions: prefix verification currently reads/hashes the
stored series per request. A writer-produced revision manifest is a possible
later optimization; do not replace verification with a stale provider cache.
