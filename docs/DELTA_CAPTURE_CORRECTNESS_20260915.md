# Delta capture correctness patch — 2026-09-15

## Scope and evidence

This is a forward-only recorder correctness patch, not a scanner revision or
an edge claim. Old candles, journals, golden artifacts and strategy IDs remain
unchanged. New raw rows and recorder metrics identify the capture contract as
`delta_event_watermark_v3`; replay comparisons must disclose the cutover rather
than pooling both capture contracts as equivalent evidence.

The VM's original coverage journal records a disconnect at
2026-09-15 05:19:12.117361 UTC and reconnect at 05:19:19.752117 UTC, followed by
the transport-timeout warning. Both BTCUSD and ETHUSD have a partial 05:19
minute, explaining the 06:00 hourly structure reset. This was not a process
restart. The preceding 05:19 watermark had already advanced past 05:19:00,
although the latest observed trade was around 05:18:36. The old wall-clock
advance could therefore publish a bucket before the transport timeout revealed
the missing interval. That is not completeness proof.

The same journals contain repeated `reorder_late_drop` faults. Code inspection
and a regression demonstrate that the periodic wall-clock drain could release
the newest print before the per-message event-time watermark permitted it.
Some late prints may also be genuinely beyond the 250 ms ordering allowance;
this patch does not assert that all drops were caused by the timer.

## Corrections

1. Periodic trade release, candle close and replay-seal checkpoints share a
   per-symbol boundary: min(wall time - 250 ms, latest trade event - 250 ms).
   No observed trade means no advancement. A quiet symbol cannot borrow another
   symbol's clock or heartbeat. No empty bars are manufactured.
   Each print must ALSO reside in the receive buffer for 250 ms before release.
   An event-time threshold alone releases the first/newest member of an
   arriving burst before its older siblings arrive. Unaged pending prints cap
   candle advancement so the periodic publisher cannot overtake them.
2. Disconnect coverage ends at the last observed transport frame, not the later
   timeout-detection timestamp. Stalled minutes remain unpublished until new
   trade evidence advances the clock and their quality is resolved.
3. Rejected input invalidates the affected still-forming minute and receipt
   minute in the canonical quality resolver, not only the replay-seal log.
   Partial minutes remain forensic rows and cannot enter the parent ladder.
4. Normalized late rejects are buffered into a separate `stream=trades_rejected`
   archive, with reason and `canonical_eligible=false`. These are never fed into
   normal replay and never certify missing venue prints. They use the normal
   bounded shard flush cadence rather than a per-print fsync.

## Frozen history and recovery limits

No existing hash or quality flag is rewritten. This explicitly accepts a
different future eligible-bar/fire set as a correctness correction. Historical
coverage faults require a separate bounded audit before those historical bars
can support promotion. Their old `ok` flag alone does not override a known
input fault. There is no complete venue trade-sequence proof for the timeout
interval, so it is not repaired from raw presence or official OHLC.

Event-time closing intentionally waits during a silent trade stream; consumers
must report stale/missing input, not invent a close. A 250 ms contract cannot
guarantee lossless capture from a stream with greater disorder. Residual rejected
prints and partial minutes must remain visible; changing the allowance requires
a separately reviewed capture contract and replay, not a silent adjustment.

## Verification and rollout

The first scoped rollout (`4613620`, capture contract v2) exposed residual
out-of-order bursts in the new rejected-trade archive: at 15:19 UTC, 23 BTC
rejects had 381–629 ms receipt age; 39 ETH rejects had 416–1470 ms receipt age.
Those ages alone are not a measured reorder allowance. Inspection showed
descending event timestamps inside bursts received within a few milliseconds.
Contract v3 therefore enforces the existing 250 ms receive-buffer residence,
without increasing the event-time disorder allowance. v2 remains frozen as its
own short capture interval; its partial rows are not upgraded by v3.

A bounded forensic replay of the received 15:18–15:20 UTC v2 archive combined
accepted rows with `reorder_late_drop` quarantine rows, retaining duplicates and
sorting stably by recorded receipt milliseconds. With the same 250 ms event
window, adding the receive hold reduced simulated late drops from 33 to 0 on
523 BTC rows and 44 to 0 on 640 ETH rows (three trailing prints left pending
in each run). This supports the burst diagnosis, not venue completeness or a
backtest result: cross-shard equal-millisecond receipt ordering is approximate,
and the old archive cannot reveal trades never received. No candles were
rewritten by this diagnostic.

Regression coverage includes delayed predecessors after timer ticks,
per-symbol isolation, timeout crossing a minute boundary, partial-bar exclusion,
separate rejected-trade persistence, and unchanged already-published identities.

Deploy only `delta-recorder` through `scripts/deploy.sh`. Its necessary restart
creates a disclosed startup seam; leave the dashboard, lanes and Binance owner
running. Verify serving SHA, advancing raw capture, quality flags, rejection
counters, and the unchanged strategy/risk posture. A healthy process or a short
clean capture sample is not a claim of a durable trading edge.

Market permission and missing continuation setups remain independent blockers.
No cost wall, regime permission, swing causality, roster, capital, or live-order
authority is changed by this patch.
