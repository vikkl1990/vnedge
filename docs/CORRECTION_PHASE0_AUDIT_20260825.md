# VNEDGE correction spec — Phase 0 verification

Baseline reviewed: `aa3f55f` (2026-08-25 working tree).

This note records what was verified in the supplied correction spec and what
was implemented without weakening VNEDGE's locked safety invariants. It does
not enable live trading or add any strategy to the capital allowlist.

## Phase 0 disposition

| ID | Verification | Correction |
|---|---|---|
| P0-1 | Confirmed | Pending-limit expiry derives milliseconds from `SessionCosts.bar_minutes`; a 15m regression test proves it. |
| P0-2 | Confirmed | Every no-candle iteration runs tick-stop, quote sync, daily flatten, stall detection, Time Machine, heartbeat, and snapshot work before quote handling. |
| P0-3 | Confirmed | Receipt latency is recorded before the canonical wait; `canonical_wait_ms` is independent. Legacy close-latency checkpoints are deliberately re-baselined. |
| P0-4 | Confirmed | One `watch_order_book` task now fans out immediate BBO updates and one-second L2 metrics. |
| P0-5 | Finding was stale | An unknown CCXT id already raised `ValueError`; it never silently fell back. A regression test freezes that stronger behavior. Valid but non-WS venues emit a warning and expose REST mode in feed health. |
| P0-6 | Confirmed as a contract bypass | Replay, L2, and lead/lag loaders now call the canonical tape cleaners and report dropped counts. The historical 62% touch-rate claim requires the referenced raw dataset and is not asserted from unit fixtures. |
| P0-7 | Confirmed | The local strategy/timeframe map was removed. Frozen scanner contracts are authoritative, with the registered strategy declaration as the legacy fallback. |
| P0-8 | Confirmed | 4h startup depth is derived from frozen MTF pivot parameters and is 12 bars with current defaults. |

## Architecture decision for Phases 1–6

The proposed five-process Redis topology is not accepted as an immediate
implementation. VNEDGE's current reviewed v1 decision is a single-process
asyncio runtime with naturally single-writer portfolio/risk state. Redis,
cross-process execution IPC, and multiple order-path failure modes require
evidence that the process boundary solves a measured bottleneck and a new
risk design review.

The useful parts remain valid as incremental seams inside the current design:

1. keep a single normalized event contract for trades, BBO, L2, and candles;
2. move CPU-heavy feature preparation off the event loop and bound frames;
3. converge live and replay scanners behind one stateful engine interface;
4. preserve one risk/WAL/order path and await actual durable journal flush;
5. keep ML veto/size-only, research-gated, and unable to originate orders;
6. add submit/ack/fill telemetry before claiming a hot-path execution SLO.

Any later process split must first pass fault-injection tests proving that IPC
loss, duplicate delivery, restart ordering, and stale intents fail closed.

