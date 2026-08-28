# Execution convergence

VNEDGE treats market time and execution authority as independent contracts.
This removes the historical ambiguity where `shadow` meant observation while
`paper` was the only simulated order path.

| Data clock | Execution stage | Meaning |
|---|---|---|
| `replay` or `live` | `observe` | Candidate and evidence records only; cannot call `OrderManager` |
| `replay` or `live` | `shadow_execution` | Canonical `OrderManager` and risk gateway with a simulated adapter |
| `live` | `live_small` / `live_full` | The same kernel with a live adapter and all three live gates |
| `live` | `emergency_reduce_only` | Live adapter; gateway permits exits only |

## Compatibility mapping

The existing configuration values remain accepted while deployment manifests
are migrated:

- legacy `shadow` = `observe`
- legacy `paper` = `shadow_execution`

Dashboard and journal telemetry publish `data_clock` and `execution_stage` so
operators can see authority rather than infer it from a historical label.

## Single submission boundary

Recorded simulated execution, live-data simulated execution, and real live
execution call `ExecutionKernel.submit`. The kernel validates that execution
authority matches adapter type before forwarding the unchanged intent and
idempotency key to `OrderManager`. `OrderManager` remains the single risk,
journal-before-submit, retry, and reconciliation boundary.

Observe-only scanners remain unable to submit. Their candidate and virtual
outcome lifecycle is evidence, not an order lifecycle. Moving an observer to
simulated execution is a registry/roster promotion into `shadow_execution`,
not a second broker implementation.

## Remaining convergence work

The active scanner observer still owns its virtual-outcome tracker. Before a
scanner is allowed into `shadow_execution`, its entry/exit contract must be
adapted to canonical `OrderIntent` and `ActiveExit`, with replay/live parity
evidence attached. No observer is promoted by this infrastructure change.
