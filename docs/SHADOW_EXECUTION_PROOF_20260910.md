# Shadow execution proof and remaining blockers

## Verdict

The isolated next-open execution path can book a simulated entry, close it
reduce-only, and project the journaled trade with its original decision and
snapshot IDs. The missing operational trade is not explained by a universal
"shadow cannot submit" switch.

This is **synthetic plumbing evidence only**, not a backtest, OOS artifact,
venue-parity result, promotion, or operational shadow trade. No fixture result
may be imported into Arena rankings, operational Desk PnL, or readiness inputs.

## Reproduce

From the repository root:

```bash
.venv/bin/python -m pytest -q tests/test_shadow_execution_proof.py
```

The tests use pytest temporary directories for all candle, journal, fill and
kill-switch files. Network connects are forbidden. The fixture strategy exists
only in the test file and is not registered or added to a roster. Its generated
candles satisfy the canonical schema and hash checks, but are explicitly
synthetic: this proves validation and identity propagation, not tape provenance.

The receipt/decision clock is fixed. Random venue attempt IDs and WAL wall-clock
timestamps are deliberately not expected to match across independent runs.

## What passed

| Case | Evidence checked |
| --- | --- |
| Accepted entry | Closed, hashed 15m fixture -> normal runtime binding -> ARM envelope -> actual CostGate -> risk-based downward-rounded sizing -> actual gateway -> kernel -> simulated fill |
| Journal ordering | ARM precedes risk decision; risk precedes journaled intent; intent precedes submission |
| Restart duplicate | A second OrderManager reconstructs the prior attempt from the WAL; submitting the same decision produces no new client ID or fill |
| Exit under kill | Entry kill is tripped after the fill; reduce-only exit still passes the normal kernel/gateway and closes the position |
| Projection | The real dashboard journal projector resolves the round trip and retains the entry decision ID, snapshot ID and clock |
| Integrity | Full decision-WAL and fill-ledger hash-chain verification passes |
| Missing hash | Runtime refuses the malformed prepared row before ARM; no order or fill |
| Missing edge | ARM exists, but `edge_estimate_missing` rejects before order creation |
| Insufficient edge | The actual CostGate rejects; no order or fill |
| Impossible sizing | Exchange-minimum fixture cannot be met within risk size; quantity is not inflated |
| Risk rejection | Entry kill produces a journaled failed RiskDecision; adapter receives no submission |
| Isolation | An operational fleet snapshot excludes the fixture lane's orders and resolved trade |

There are seven pytest cases (one cost test has two parameter cases; several
invariants are verified within the accepted round trip).

The fixture uses the existing Delta scalp CostGate and Delta venue fill-model
factory, plus explicit synthetic quantity limits. The positive 100-bps edge
input is a test constant named `fixture_only_not_oos`, not an economic estimate.
Neither quantity limits nor fee/slippage assumptions are verified against a
live venue by this test. BBO is supplied at the next interval boundary; no
queue position, quote-hold sequence, network delay or future price path is
inferred. The exit is explicitly requested to test the reduce-only boundary;
this does not validate a strategy exit detector.

## Why production still has no shadow trade

Observed on VM build `21d3fdc` at the September 10, 15:45 UTC close:

- BTC and ETH HTF lanes each had 490 persisted evaluations (283 live), zero
  signals, zero submissions and zero fills. These are retained funnel counts,
  not a claim of a complete lifetime audit.
- Each had 800 daily context observations, EMA200 ready, a valid canonical
  decision hash, valid bound context and valid hourly parent identity.
- Both classified `mean_revert / weekly_range_macd_off`, which denies the
  deployed continuation hypothesis.
- Both had one confirmed high and zero confirmed lows since their last
  quality reset. The required structure pair was not ready.
- Both deployed pair contracts had `edge_model_id=null` and
  `oos_gross_edge_bps=null`. A setup that passes the earlier gates would still
  encounter the missing-edge rejection demonstrated above.

The roster contains three measurement lanes and two HTF continuation lanes,
not an independently validated short-horizon scalper. Capital and private
venue-stream gates remain necessary for live execution; they are not reasons
the isolated simulated path cannot fill.

## Next engineering/research boundary

1. Recover only verifiable missing tape and retain honest coverage failures.
2. Obtain a genuine versioned edge artifact for a candidate, or reject it.
   Never substitute target distance, this fixture's 100 bps, or an invented
   OOS number. The test documenting current null HTF fields must be revised
   alongside any reviewed artifact binding, not bypassed to make CI green.
3. Preregister a separate short-horizon hypothesis if the objective remains
   scalping. Freeze its clock, fill assumptions, horizon, data requirements
   and costs before results. Leave HTF v2's fire set unchanged.
4. Validate the selected candidate on real tape and its actual entry path;
   then collect forward-shadow evidence through the same kernel.

This slice changes tests and this report only. No execution gates, strategy
parameters, registry entries, production files, UI, or capital settings change.

## Validation

- New proof suite: **7 passed** (rerun after final fixture formatting and
  Delta fill-model selection).
- Full local Python suite: **2,962 passed, 6 skipped**; existing dependency
  deprecation warnings were reported.
- Ruff check on the new test file and `git diff --check`: passed.
- The full suite ran with pre-existing local Arena/recorder work present;
  those changes are not part of this proof slice.
- No VM deployment or Git commit was performed by this slice.
