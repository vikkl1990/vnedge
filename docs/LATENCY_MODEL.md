# Tick-fast data, bar-slow decisions

VNEDGE receives market events continuously, but a strategy may speak only on
its registered decision boundary. Fast receipt is not permission to evaluate a
closed-bar rule early.

```text
exchange event ──► normalize / tick lake / forming state ──► health + stop checks
                                  │
                                  └── canonical bucket closes
                                           │
                                           ▼
                                  closed-bar decision queue
                                           │
                                           ▼
                              prepare → strategy → gates → journal
```

For `structure_bos_1h`, the queue receives only closed 1h candles. Confirmed
L=R=3 swing anchors become usable three closed hours after the pivot. A 4h
higher-timeframe reading changes only after its UTC-aligned 4h bar closes. Tick
updates may refresh the Pulse, stream health, forming state, and protective
stops; they cannot emit an S1 entry intent.

## Canonical measurements

| Metric | Start → end | Meaning |
|---|---|---|
| `ingest_lag_ms` | exchange event time → local receipt | Tick-path transport and receipt latency |
| `bar_close_processing_ms` | canonical bucket close → decision-loop dequeue | Exchange close emission, queueing, and runtime scheduling |
| `decision_lag_ms` | closed bar in hand → evaluation complete | Monotonic strategy compute time |
| `clock_skew_ms` | future exchange/close time → local UTC | Magnitude of a future-clock observation |
| `feed_lag_ms` | same as `bar_close_processing_ms` | Temporary compatibility alias |

`bar_close_processing_ms + decision_lag_ms` is the closed-candle-to-decision
path. `ingest_lag_ms` is intentionally not added to that value: a bar close is
a separate causal boundary, and double-counting tick transport would make the
number meaningless.

Naive datetimes are rejected. A future timestamp never becomes a negative
latency sample; latency is clamped to zero and the future magnitude is recorded
as `clock_skew_ms` for data-quality handling.

## Runtime policy

The shared budgets in `vnedge.runtime.latency_thresholds` apply to the closed
bar path:

| Condition | Soft | Hard |
|---|---:|---:|
| Closed-bar processing rolling p95 | 500 ms | 2,000 ms |
| Decision compute rolling p95 | 50 ms | 200 ms |

A soft breach degrades the dashboard. A hard breach blocks new arms while
exits, reduce-only actions, kill handling, journaling, and observation remain
available once the rolling p95 has at least 20 samples (the minimum population
with a meaningful 5% tail). Earlier samples remain visible but do not halt
arms. Stream-stale thresholds remain feed-specific because websocket and
REST-polling feeds have different honest cadence; they must be configured at
the feed guard instead of inferred from the strategy timeframe.

## Heartbeat semantics

These events must not be conflated:

- `waiting_for_closed_candle`: the loop is alive and may be receiving ticks,
  but no new decision bar exists.
- `bar_processed`: a new closed decision bucket was accepted and processed.

Therefore an active websocket during a quiet hour can prove process/feed
liveness without claiming that a 1h strategy made a new decision. This is the
intended architecture: real-time market awareness with causal, bar-close
strategy intent.
