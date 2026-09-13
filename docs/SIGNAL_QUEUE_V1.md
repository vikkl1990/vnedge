# Signal Queue v1 — implementation scope

The first slice of SIGNAL_QUEUE_ML_LAB_ARCHITECTURE_20260913 is implemented.
This is a read-only recent-journal queue, not the full ML Lab or a second book.

## Surfaces

- React `#signals`: armed decisions (including orders), evaluations, orders/fills,
  research observations and all records. Filters: strategy, market, clock, mode.
- `/api/signal-queue`: bounded cursor-paginated read projection, default 25/max100.
- `/api/signal-queue/{row_key}`: source timeline, original validated envelope,
  snapshot/bar identity, cumulative order state and recorded quote metadata.
- Existing viewer authentication applies; no POST, training, order or promotion
  operation is added. Public read-only viewers remain read-only.

## Identity and accounting

`DecisionEnvelope.from_dict` verifies ARM proof. Missing proof never creates a
decision_id. Diagnostic IDs are namespaced display identities only. Conflicting
IDs, envelope fields, venue-ID ownership or instruction hashes fail visible.

Orders count as operationally linked only with validated envelope plus matching
journaled order instruction and submission. Positive cumulative fill quantity
requires a fill/reconciliation/cancel report; ACK is not a fill. Duplicate
records cannot increment counts or fill quantities. An order resolved by
reconciliation is not a closed trade. Partial cancellations remain explicit.

No booked net is computed by this queue. Existing Book remains the accounting
view. ShadowOutcomeTracker numbers are labeled research simulations and never
count as fills. No ML probability or setup grade is fabricated.

## Index and performance boundary

The disposable `.dashboard_signal_queue_v1.sqlite` lives next to lane journals.
It writes no source files. Bootstrap reads the latest 512 KiB per active scanner
lane; subsequent refreshes ingest at most 512 KiB per lane and only complete
lines. At most 32 lanes and 2,000 normalized events per lane are indexed.
Retired/measurement lanes are excluded; no active roster means no fleet history.

A single in-process lock serializes SQLite transactions. All reads/index work
run through `asyncio.to_thread`, not the scanner event loop. Cache lifetime is
five seconds. Missing sources, malformed records, incomplete lines, overflow
and lane limits are reported. No exception is allowed to imply an empty book.

Source inode, size and a 128-byte checkpoint detect rotation/truncation and a
changed checkpoint. A new generation discards that source's old projection.
This is not full-file chain verification; offline journal verification still
owns that assurance. The index can be rebuilt without changing trading state.

History is always labeled bounded/incomplete. If an ancestor falls outside the
window, descendants cannot establish an operational order chain. This can
under-count long-lived orders; inspect the existing Book/full audit for those.
There is no lifetime-trade-count or combined PnL headline here.

Cursors bind the projection revision, filters and page size. Changed evidence
returns 409: refresh the first page instead of silently skipping/duplicating
rows. Older pages pause auto-refresh and say HISTORY PAGE. Source event time is
shown separately from index refresh time. List/detail responses are allowlisted
projections, not arbitrary raw WAL payload dumps.

## Verification and rollout

Tests cover no-proof fires, exact ARM validation, contradictory identities,
missing instructions, changed order geometry, cumulative partial fills,
reconciliation/cancel states, research PnL isolation, incremental restart,
incomplete tails, malformed lines, rotation, active-roster scope, pagination,
authentication and unsupported writes. Frontend tests pin null/UTC/outcome
presentation and the read-only empty state.

The preview uses local test fixtures only; no fixture enters production journals.
Deploy only the dashboard/runtime image after review; recorders do not change.
The dashboard process must have write access to the journal directory for its
disposable index. An index error returns 503 and a visible UI error.

## Deliberately remaining

- Full-history background projection and archival cursor management.
- Exact feature-log operational dataset wiring and label-availability audit.
- ML training/calibration/walk-forward job UI and immutable prediction display.
- Complete entry/exit/funding accounting joins (owned by Book, not guessed here).
- Dedicated ARM/ACCEPT producers where an existing runtime does not yet journal
  those stages; the queue never infers acceptance from a chart or quote alone.

No strategy, cost, risk, capital, roster, data authority or live-adapter changes.
