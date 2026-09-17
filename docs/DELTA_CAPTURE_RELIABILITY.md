# Delta capture reliability — September 17 local changes

Capture status reports each symbol independently. Missing/stale producer reports,
disconnects, future timestamps, and stale symbol trade timestamps fail the capture
probe. This is a conservative active-market liveness test, not proof of loss.
Historical coverage comes from the separately timestamped recovery plan; reconnect
cannot clear historical gaps. Scanner readiness remains owned by per-lane gates.

Connection transitions wake the existing single-owner repair/audit cycle. Repairs
still require original sealed evidence or verified children. This does not fetch
absent historical venue trades. No OHLC fallback or synthetic proof is allowed.

The owner runs an isolated alert task with a journal at
`data/reports/delta_capture_alerts.jsonl`. Capture problems repeat at most every
five minutes; unresolved/unknown coverage repeats every thirty minutes. Telegram
delivery is available only when its existing environment credentials are supplied
to this service. Console disconnect errors are immediate. Notification network
calls run off the recorder event loop. No notification delivery has been verified
by these local tests.

Not implemented in this slice: independent backup capture, verified external
trade backfill, or retroactive repair of unseen September prints. The public
recent-trade endpoint cannot by itself certify a complete disconnected interval.
Do not interpret this patch as restored history or guaranteed zero-gap recording.
No live permission, scanner threshold, or canonical admission rule is changed.

## Analyst storage recovery

Full Analyst evidence databases are preserved in place. New writes continue in
numbered sibling SQLite segments; reads merge and hash-verify records across all
segments, preserving point-in-time cutoffs. The worker's existing exclusive lease
remains required. Segments retain the 1 GB bound, with at most 32 segments and a
1 GB free-space floor before opening another. Reaching these bounds still refuses
writes and requires archival; the change is not unlimited disk growth. No old
evidence is deleted or rewritten by rollover. Both dashboard and worker must be
deployed together so new segments are visible to readers.

The command bar prioritizes data/decision failures above unproven execution
evidence and labels each scope explicitly. All underlying gates remain unchanged.
