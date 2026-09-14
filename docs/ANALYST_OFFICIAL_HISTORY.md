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
OHLC endpoint, using 5m, 15m, 1h, 4h and 1d. Initial coverage: BTC, ETH, SOL, DOGE,
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
is inferred. The profile selector retains its four original timeframes; daily
history is used only for the separate Market Stage product below.

Historical OHLC fetched today is available to this Analyst today, not a
point-in-time historical feature archive suitable for ML promotion.

Collector heartbeat health means the process is running, not that all 60 cells
are ready. Per-cell failures are logged; series gaps and stale data remain
visible. The append-only store has a 1 GB hard bound: archive under an explicit
retention procedure before reaching it; do not silently prune evidence.

Verify `/api/crypto-analyst?exchange=delta_india&timeframe=15m&source=official_delta`
and the same endpoint with `source=canonical` independently after deployment.

## Market Stage and observation history v1

`market_stage_official_delta_v1` describes 4h/1d stages using an independently
validated official snapshot. The existing canonical version and scanner IDs
remain unchanged. Shared classification mathematics: EMA50 (first-close seed),
five-bar EMA slope divided by rolling-14 true range; 20-bar displacement and
prior-only 20-bar range width in the same ATR units. Two consecutive proposed
labels confirm a state. Confirmed 3/3 pivots are diagnostics only.

Above EMA with slope >0.15 and displacement >=2 means advancing; below EMA with
slope <-0.15 and displacement <=-2 means declining. Absolute slope <=0.15,
absolute displacement <=1 and width <=6 mean a flat range: the preceding
confirmed decline supplies base-after-decline memory; preceding advance supplies
range-after-advance memory. Without that memory it is unknown. Other conditions
propose transition. These are descriptive thresholds, not a validated edge.
Watch explanations state these same rules, not an additional breakout gate.

Every run reconstructs up to 512 bars, requiring at least 60 contiguous valid
bars. Gaps reset the usable suffix and its trend memory; insufficient suffixes
are unavailable. Staleness is 1.5 timeframe durations. No automatic forward-fill.
The bounded initial seed makes duration left-censored; rolling/revised snapshots
can change reconstructed transitions. Those transitions never masquerade as
point-in-time live events or promotion-grade ML labels.

The history collector records one immutable `official_market_stage_v1` report
per new source evidence ID. Its source hash, version hash, receipt time, recording
time and preceding report ID are retained. Baseline, unchanged, reported stage
change, observation gap, coverage change and source revision remain distinct.
API readers require the report to match the latest source; pending or failed
classification cannot carry an old report as current. Earlier reconstructed
transitions live inside the report collected today, never as backdated reports.
Both dossiers and saved history expose this distinction. All outputs remain
`can_trade=false`, `can_promote=false`; no scanner/ML/risk consumers are connected.
