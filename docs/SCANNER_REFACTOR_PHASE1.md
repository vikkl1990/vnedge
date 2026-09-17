# Scanner refactor: explanation truth and offline paper-path diagnostics

Scope: local implementation, no deployment or live/capital authorization.
HTF v2 parameters, market-regime rules and the runtime roster remain frozen.

## Delivered

- `DecisionContext` is a frozen, versioned projection of one evaluation's
  recorded features, bar identity, as-of context, permissions and failures.
  It does not calculate a second regime or grant any permission.
- `regime_flat` remains the original machine gate. The explanation distinguishes
  unproven context, a mean-reversion/continuation family mismatch, and evaluated
  but disallowed conditions. Unknown values are not converted into healthy ones.
- Journal enrichment, signal queue, runtime-lane API and workbench use the same
  explanation function. Current operational blockers remain separate from the
  last evaluation, whose decision timestamp remains attached.
- An evaluation can report watching, setup/break detection or signal detection.
  It cannot claim ARM, quote acceptance, CostGate approval or a shadow intent.
  Those require their separate evidence records. Legacy event enum values remain
  available for actual downstream event consumers.
- Strict canonical paper replays refuse a pending next-open entry if the next
  available row is later than the declared clock. Legacy non-strict replay
  behavior is unchanged.
- Sizing rejections and entry-clock decisions are journaled. Paper exits now
  carry entry and exit managed-order IDs to link the round trip.

## Offline diagnostic command

```sh
.venv/bin/python -m vnedge.research.paper_path_replay \
  --strategy htf_regime_continuation_15m_v2__BTCUSD \
  --candles /absolute/path/to/btc-15m.parquet \
  --h4 /absolute/path/to/btc-4h.parquet \
  --daily /absolute/path/to/btc-1d.parquet \
  --output /absolute/path/to/new-diagnostic-run
```

Only the two frozen BTC/ETH HTF registrations are accepted. No network or venue
client is constructed. All three explicit inputs must be consecutive, closed,
quality-ok canonical rows with matching persisted hashes, coverage and exact
`delta_india` / `BTC/USD:USD` or `ETH/USD:USD` market identity. The tool never
generates provenance, restores missing rows, or imports an untouched judgment
window automatically. The operator must supply an already-exploratory dataset.

This diagnostic intentionally requires canonical HTF inputs, stricter than v2's
declared validated-exchange HTF context allowance. It is not a claim of full
input parity with the deployed lane. Source-policy parity remains a separate
acceptance item.

The tool creates a new run directory containing input file hashes, frozen run
configuration/cost assumptions, a chained decision journal and a summary.
It refuses to overwrite an existing directory. It uses `PaperRunner`, the
execution kernel, sizing, CostGate, gateway, OrderManager, PaperBroker and
reconciler—not a second implementation of order execution.

`MECHANICS_OBSERVED` requires linked ARM/clock/cost/risk/entry evidence, recorded
fills with fees, a linked final exit, flat final positions and a clean journal
chain/reconciliation. Empty runs and rejected candidates are `INCOMPLETE`.
Modeled spread/slippage and GST-inclusive profile fees are assumptions, not
measured execution. Funding settlement is not verified/booked in this replay.
Every report has `can_trade=false`, `can_promote=false`, and
`performance_eligible=false`, even after a mechanically successful round trip.

## Not completed / not claimed

1. A genuine historical qualifying setup has NOT completed this new diagnostic.
   Synthetic fixture round trips test mechanics only and stay in test directories.
2. Both active HTF pair contracts currently have `oos_gross_edge_bps=None` and
   `edge_model_id=None`. A signal alone therefore cannot pass the strict CostGate.
   The tool reports this missing support; it never substitutes target distance
   for expected edge or invents an estimate.
3. Settled funding, historical BBO/fill realism, verified complete historical
   inputs and source-policy parity remain prerequisites for economic validation.
4. The three proposed strategy families, regime/session ablations and learned
   scoring are later phases, not implemented or activated here.
5. No live-client, capital allowlist, risk limit, data-quality gate or deployed
   scanner threshold was changed. Existing unrelated private-stream edits remain
   outside this slice.

## Regression acceptance

Tests cover context/queue/cockpit agreement, strict unknown handling, no fabricated
execution lifecycle, input tampering/duplicates/gaps, killed-strategy refusal,
next-open refusal and linked synthetic paper round trips. Existing runtime
execution-proof tests cover restart deduplication, cost/sizing/risk rejection,
and reduce-only exits through the real paper-session/kernel path.
