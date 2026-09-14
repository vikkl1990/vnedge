# UI and backend audit — 2026-09-14

Scope: read-only production diagnostics, browser exploration of the local `/app`
build, and isolated regression tests. Base commit `1ffa0932`; the workspace also
contains prior, uncommitted Analyst/research work. No strategies, roster, capital,
costs, credentials, live permissions, or production services were changed.

## Confirmed defects and corrections

| Defect reproduced | Correction |
| --- | --- |
| Book summed absent shadow telemetry into `$0.00` | Unknown, incomplete or stale lane totals show unavailable; genuine reported zero remains zero. Measurement lanes do not enter the shadow sum. |
| Journal displayed zero totals and “matched” without source history | Backend now publishes bounded source coverage, missing active lanes, unreadable files and malformed records. UI withholds totals on partial/unknown coverage. Arithmetic agreement is explicitly not ledger reconciliation. |
| Header said no blocker, kill clear and halt clear without operational evidence | Missing/stale snapshots withhold readiness passes. Missing risk evidence is unknown; incomplete readiness is not an all-clear. |
| Tape spun on “Loading canonical lake” when there was no selected lane | Shows unavailable, no eligible lane, and history not requested. No fallback market or candles are inserted. |
| Arena counted 200 `DONE_RESEARCH_ONLY` jobs as active proofs | Counts only explicit queued, pending and running states. Terminal and unknown states are not active. |
| Forward Queue converted absent outcome records into zero progress | Missing counts are dashes with an evidence-gap message, not simulated zero trades. |
| Analyst dossier cache could outlive technical/stage/fundamental expiry | Cached current observations are revalidated against their own clocks before reuse. |

Numeric journal summaries remain diagnostic sums of readable records for compatibility.
Consumers must inspect `source_coverage`; it never claims full-history or ledger
verification. A valid empty journal differs from a missing/unreadable journal.

## Browser coverage

Local preview: `http://127.0.0.1:8095/app/`.

- All ten main views: Analyst, Strategy, Monitor, Signals, Tape, Book, Evidence,
  Data, Arena, Settings.
- Arena: all eight sections, including The Book, Pipeline, ML Lab, Backtests,
  Forward Queue, Campaigns, Agent Scoreboard and Failure Archive.
- ML Lab: Overview, Datasets, Features, Validation, Forward feedback.
- Signals: all five population choices; empty/partial source behavior.
- Analyst: overview/scanner/watchlist/brief; search; add/remove BTC watchlist;
  six named scanner categories and reset; three venue choices; four analysis
  timeframe choices; Stage/fundamentals, Multi-timeframe, Ask, History tabs;
  a missing-evidence answer; public conditions and fundamental attribution.
- Read-only risk drawer open/close; command-palette discovery, search and keyboard
  navigation to Book. Local session reauthentication after preview restart.
- Rebuilt Book, Evidence, Tape and Arena/Forward Queue were revisited and the
  corrected states verified in the rendered page. Default narrow viewport was
  visually inspected; no separate desktop/mobile breakpoint matrix was run.

This is not a claim that every possible filter combination or privileged action
was exercised. Settings correctly required operator permission. Research launch,
credential changes, risk mutations, capital activation and order submissions were
not performed. Real fill drill-down and fully populated canonical charts need
eligible data; this local preview deliberately has no operational snapshot or
canonical candle history. No data was fabricated to populate it.

## Production observations

Read through the existing SSH connection to `vn-edge-sg-01` (`161.118.252.185`).
Fourteen Compose services were running. Services with health checks reported
healthy; services without health checks were only verified running, not proven
end-to-end healthy.

All 27 sampled HTTP endpoints returned 200:

`/health`, `/ready`, `/state`, `/api/lanes`, `/api/services`,
`/api/risk/snapshot`, `/api/scanners`, `/api/patterns`, `/api/signal-queue`,
`/trade-journal`, `/session-regime`, `/research`, `/backtest-lab`, `/cost-model`,
`/agentic-research-os`, `/research-pipeline`, `/agent-jobs`, `/api/ml-lab`,
`/ml-status`, `/meta`, `/fleet`, `/data-products`, `/scanner-evidence`,
`/quote-parity`, `/strategy-workflow`, `/scorecard`, `/whoami`.

The sample measured `/session-regime` at about 3.6 seconds and `/trade-journal`
at 0.73 seconds. These are individual API request times, not a latency percentile
or scanner decision-compute measurements. HTTP 200 is not proof of valid trading
evidence: the research pipeline reported `BLOCKED_EVIDENCE`.

BTC and ETH both reported fresh lane snapshots, 800 daily bars, evaluated
closed bars, EMA200 ready, and no missing context timeframes. Both still reported:

- `canonical_transport_parity_unproven`
- `kernel_envelope_audit_unproven`
- `capital_path_locked`
- `venue_private_stream_unavailable`

These checks were not weakened. ML's worker artifact was current under its own
published contract; that does not establish sufficient reconciled labels or
promotion-grade validation.

Production browser navigation failed with `ERR_CERT_AUTHORITY_INVALID` on the
IP-address HTTPS URL. No browser security warning was bypassed. A trusted TLS
certificate/hostname remains a production access issue; the SSH HTTP checks do
not validate browser trust.

## Validation and handoff

Regression coverage: `tests/test_dashboard_audit.py` and
`frontend/src/components/DashboardAudit.test.ts`, plus existing trade-journal,
Analyst and chart tests.

- Full Python suite: 3,197 passed, 6 skipped (179.25 seconds).
- Frontend suite: 68 passed across 12 files.
- Production frontend build passed; the existing large Vela chunk warning remains.
- `git diff --check` passed.

Fixes are local and uncommitted. Production is unchanged. A future deployment
must ship the Journal API source-coverage addition together with its frontend;
the new frontend treats an older API's missing coverage field as unknown.
