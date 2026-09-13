# ML Lab v1 — evidence audit, not automatic training

Arena now has an ML Lab with Overview, Datasets, Features, Validation and
Forward feedback views. It reads `/api/ml-lab`, which projects the existing
`ml-pipeline-status` worker's audit artifact. All controls are read-only.

## Correction shipped

The status worker formerly rebuilt features from entry-bar candles, fell back
by symbol, included legacy simulated outcomes, and invoked the model evaluator
on those samples. This path no longer contributes operational labels or runs
training. Existing exploratory builders remain available to explicit research
callers; no strategy/model/roster is changed by this release.

The replacement audits recorded `.features.jsonl` and `.journal.jsonl`:

- Exact lane + decision ID, validated ARM envelope, bar hash, side, strategy,
  timeframe and bar-open comparisons. A match is diagnostic, not label approval.
- Duplicate records do not increase matches; conflicting feature contracts
  for a decision are excluded. Features from another lane never match.
- Finite feature completeness and per-column missingness, split into feature
  cohorts by strategy, exchange, symbol, timeframe and fingerprint.
- Explicit exclusions for backfill, invalid proof, missing features, research
  outcomes, exit requests without net accounting and post-decision features.
- Separate worker-generated and source-event timestamps. A fresh status job
  does not refresh its input evidence. Legacy artifacts are unverified in Lab.
- Atomic artifact replacement; up to 64 files, 1 MiB tail per file, no candle
  scans in the worker or HTTP handler. Partial/malformed sources stay visible.

## The important remaining implementation gap

`live_paper_exit` and `tick_stop_exit` describe exit submission/state, not a
final reconciled position outcome with entry and exit fills, all fees, funding
and net return. This release does **not** build that accounting finalizer.
Consequently operational labels remain zero with
`label_status=LEDGER_JOIN_NOT_IMPLEMENTED`, even if exit rows claim a positive
net or performance eligibility. The Lab states the implementation gap; it does
not misreport it as "just wait for 200 trades".

The existing feature writer computes in the background after evaluation. Its
recorded vector may be useful for declared retrospective research, but it is
not proof that a model prediction existed before that decision. No historical
prediction or calibrated probability is synthesized.

This bounded audit is not a training dataset, lifetime trade count, operational
book or complete chain verifier. Feature cohorts still lack bound cost, entry
clock and outcome policy. Model and validation views explicitly remain unbound;
existing unrelated model artifacts are not silently imported or deleted.

## Next ML build

1. Reconciled entry/exit/fee/funding outcome finalizer, exact entry decision
   linkage, maturity/censoring and availability clocks, cost/mode partition.
2. Versioned dataset manifest and frozen train/calibration/test windows;
   purge overlapping outcome intervals in event time, not sparse row offsets.
3. Bounded operator-authorized training job with separate calibration and
   later untouched validation; immutable models, baseline/cost comparisons.
4. Report-only forward predictions frozen before outcomes; human promotion
   review remains separate. No UI click enables capital or changes scanners.

Training floors remain 200 labels, 300 for CPCV entry and 200 per usable train
fold. Counts alone do not authorize fitting, establish profitability or promote.

Rollout scope: multi-lane-shadow (dashboard), ml-pipeline-status, dashboard-tls.
Canonical recorders remain untouched. No venue credentials are read by audit.
