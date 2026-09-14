# Range-expansion baseline test — 2026-09-10

## Verdict

**UNTESTABLE EDGE — insufficient verified warmup overlapping the BBO window.**

Ran the unchanged `range_expansion_realtime_v2` through the existing research quote engine on BTC and ETH Delta India captures from **2026-09-04 00:00–2026-09-05 00:00 UTC**, using preceding verified candles. This is an exploratory mechanism replay, not a kernel execution test or promotion judgment.

| Measurement | BTCUSD | ETHUSD |
| --- | ---: | ---: |
| Captured quotes replayed | 847,070 | 849,033 |
| Engine-distinct quotes | 297,202 | 282,738 |
| Verified 15m bars through the window | 384 | 383 |
| Required warmup bars before an arm | 2,017 | 2,017 |
| Decision closes in audited window | 96 | 95 |
| In-session `hour_profile_not_ready` | 16 | 16 |
| Outside-session evaluations | 80 | 79 |
| Arms / entries / outcomes | 0 / 0 / 0 | 0 / 0 / 0 |
| Return, PF, expectancy | N/A | N/A |

The scanner requires a 20-day hour-of-day profile, with a frozen 2,017-bar warmup (approximately 21 days). Its `realtime_arm` rejects before that index. Therefore these quotes cannot establish whether an otherwise eligible setup would clear hold, cost, sizing, risk or execution.

Zero outcomes are **not** zero-return evidence. Engine JSON contains zero-valued accounting defaults; do not put those on a performance scoreboard. Quote contract rejection counters are distinct from CostGate/risk rejections.

## Data checks

- Used the exact range-v2 lane capture directories, not a second public-book recorder. Verified lane, exchange and symbol fields; replay preserved captured rows.
- No recorded capture overflow, stream overflow, locked or crossed quotes in this window. This does not prove uninterrupted upstream feed coverage.
- Checked stored candle hashes against the canonical hash implementation, UTC 15m alignment, close identity, explicit closed status, canonical source, quality and coverage flags.
- Excluded 29 unverified/partial August 31 rows per symbol. No source, hash or close proof was fabricated.
- ETH lacks the **September 4 23:30 UTC** 15m bar. No interpolation or calendar-gap splice was applied. Insufficient warmup already prevents every arm, independently of this gap.
- Even the copied current lake through September 10 contains only **915 BTC / 914 ETH** rows passing these stored-proof checks. Future rows were excluded from the September 4 replay.
- Stored hash verification does not independently establish raw-tape completeness or prove that the current candle revision equals the historical revision seen by the lane. Historical live/replay identity parity remains unproved.

## Older alternative checked

The July 7–August 2 research inputs contain 2,389 BTC / 2,385 ETH bars but lack `source`, `content_sha256`, `coverage_ok` and `is_closed` columns. No matching BBO was supplied for that window. They were **not replayed or stitched into September**. Their row counts alone cannot establish valid warmup or execution evidence.

## What this establishes

The selected baseline is not ready to test economic edge on these inputs. In particular, it was **not rejected by costs**: it never armed. This does not establish or refute the proposed range-expansion edge, and says nothing about the separate current HTF-v2 regime denial.

The next valid test needs a provenance-verified, sufficiently complete warmup before a matching quote window, followed by nonzero acceptance/rejection evidence through the risk and kernel path. Keep cost assumptions explicit; no short-duration fee waiver was applied here. Funding and kernel/account-state replay remain missing.

Do not lower warmup, splice official candles as canonical tape, add filters, or promote the strategy from this result. A shorter-history claim would be a separately registered experiment.

## Reproduction and scope

- [BTC summary](BTCUSD_summary.json), [BTC raw replay](BTCUSD_replay.json)
- [ETH summary](ETHUSD_summary.json), [ETH raw replay](ETHUSD_replay.json)
- [Input/source hash manifest](input_manifest.json)
- Local runner: `/tmp/vnedge-range-baseline.U7rGgi/run_baseline.py`; copied inputs remain in the same temporary directory.
- Regression command: `.venv/bin/python -m pytest -q tests/test_realtime_scanners.py tests/test_scanner_evidence.py tests/test_range_expansion_observer_v3.py`
- Result: **51 passed**. Unit tests are software checks, not economic evidence.

No scanner parameters, roster, live permissions or VM services were changed. No orders submitted. `can_trade=false`, `can_promote=false`, `performance_eligible=false`.
