# Official Delta history — Analyst-only contract v1

Approved 2026-09-14. This is a separate descriptive data product, NOT a repair
of canonical trade-lake history or a new scanner. Strategy IDs, roster, CostGate,
ML labels and capital permissions are unchanged.

The Delta Analyst UI defaults to **Official Delta history**. Its explicit source
selector also offers **Canonical trade lake**; API requests default to canonical
for backwards compatibility. No automatic source fallback occurs. Dossiers and
answers follow the selected source, including separate cache identities.

## Bounded backfill and refresh

`analyst-history` owns `data/analyst_official/evidence.sqlite` under a single-writer
lease. It requests 512 closed bars per cell from the public Delta India historical
OHLC endpoint, using 5m, 15m, 1h and 4h. Initial coverage: BTC, ETH, SOL, DOGE,
XRP, ADA, AVAX, LINK, LTC, BCH, BNB and DOT (USD contracts). Public ticker discovery
can show more markets, but those are not claimed as technical-history coverage.

The worker checks every minute and fetches only when a cell has a new closed
window. Snapshots are append-only, content-hashed, timestamped at receipt and
never written into `data/candles`. Dashboard reads are local and read-only.
The container has no credential file or canonical candle mount. Provision the
directory as the deployment user before enabling the `analyst` profile.
This store uses SQLite rollback journaling (not WAL), so a physically read-only
dashboard mount can read without creating shared-memory sidecars. Existing
Analyst stores retain their original journal mode.

Rows must be finite, positive OHLC, valid OHLC geometry, aligned UTC opens,
nonnegative venue volume and closed at the request boundary. Conflicting
duplicates reject the response. Missing bars are never padded. Profiles require
60 contiguous bars and become stale after 1.5 timeframe durations from the last
close. Hash/source checks in the canonical reader remain unchanged and reject
these official rows. Changed official snapshots create new evidence hashes.

## Meaning and limitations

EMA alignment, momentum, prior-range breakouts, volatility compression and
relative venue-volume are descriptive observations. They are not probabilities
or demonstrated net edge. Official volume is not trade-converted base/notional;
exact session VWAP is always unavailable in this mode. No institution identity
is inferred. Daily market stages and saved stage-change histories remain
unavailable for the official mode until independently validated.

Historical OHLC fetched today is available to this Analyst today, not a
point-in-time historical feature archive suitable for ML promotion.

Collector heartbeat health means the process is running, not that all 48 cells
are ready. Per-cell failures are logged; series gaps and stale data remain
visible. The append-only store has a 1 GB hard bound: archive under an explicit
retention procedure before reaching it; do not silently prune evidence.

Verify `/api/crypto-analyst?exchange=delta_india&timeframe=15m&source=official_delta`
and the same endpoint with `source=canonical` independently after deployment.
