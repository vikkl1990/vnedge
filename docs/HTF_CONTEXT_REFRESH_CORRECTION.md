# HTF context refresh correction — 2026-09-10

Production BTC/ETH evaluations at 02:15 UTC rejected with `htf_context_missing`.
Both lanes had loaded 800 daily and 800 four-hour history rows at startup.
The latest required canonical 4h (September 9, 20:00 UTC) and daily (September
9, 00:00 UTC) rows were absent. Four minutes in that four-hour window were
correctly marked `partial`, `coverage_ok=false`: 20:08, 21:08, 22:47, 23:06.
Recorder logs show a websocket interruption at 20:08:05; recorded prints
alone do not establish completeness across the interruption.

## Correctness defect

HTF v2 and its BTC/ETH cells already register `exchange_ohlcv_validated` as a
permitted **HTF permission** source. Startup used this contract, but live
refresh demanded canonical tick-lake rows only. It waited eight seconds for
each missing TF, then invalidated the previously valid context.

## Correction

- Reuse an exact, closed, validated bound identity where available.
- Otherwise read the exact canonical row first. For an explicitly opted-in
  context contract only, fetch the exact official closed HTF row with a
  bounded timeout, validate geometry, identity, source and hash, and bind it
  with its original provenance. Never carry an older parent forward.
- Preserve the warm-up prefix. Journal the bound source, hash and OHLCV;
  never write official rows into the canonical lake or give them tape VWAP.
- Missing, corrupt, forming and timed-out inputs remain unhealthy and retry
  at the next closed decision. Canonical-only contracts have no fallback.
- Drought reports `context_unhealthy` for current context/parent failures,
  even if the historical primary-gate histogram is mostly `regime_flat`.
- Startup readiness no longer compares bound frames against `NaT` or reports
  zero daily rows merely because the first evaluation has not happened yet.
  Decision identity and EMA readiness still require evaluation evidence.

No strategy parameters, scanner contracts, roster, CostGate, execution
authority, capital permissions, or 15m decision-source policy changed.
This is a runtime conformance patch to the existing context contract, not a
new detector. Replay classifications can differ from the broken refresh path;
old artifacts and their deployment attribution remain unchanged.

## Not repaired or claimed

Historical partial minutes remain partial. Complete-parent-only rollups and
Arena's canonical history/coverage admission checks remain unchanged. A
healthy context can still yield a legitimate regime/structure/setup denial.
No new signal, order, profitable edge, or live readiness is promised.
