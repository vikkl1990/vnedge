# Release 86e8a65: deployment and backtest

## Deployment

- Code committed and pushed to `origin/main`: `86e8a65`.
- Deployed image: `sha256:3033f03772c07b89a35ab2f7c519540cf2735eb4a194bc11d102c25d591438d2`;
  Python 3.12.14, pandas 3.0.5.
- VM: `vn-edge-sg-01`, application reports the same build.
- Updated consumers: multi-lane-shadow, research-loop, agent-job-runner,
  scanner-evidence, quote-parity-evidence.
- Delta and Pulse recorders were not restarted. Both retained their
  2026-09-08 17:57 UTC start times; application restarted at 18:27 UTC.
- `/healthz`: 200; `/ready`: 200 after startup. Fleet policy: safe, five
  lanes checked, no findings.
- Capital enabled: 0; capital strategy: empty; orders/live orders allowed:
  false. The roster is unchanged.
- Data/decision readiness is not live readiness: transport parity,
  kernel-envelope audit and private venue stream remain unproved/unavailable.

## Method

This is an exploratory release regression, not an OOS judgment. No tuning,
promotion, roster edit or order placement was performed.

The isolated backtest container has no network, no venue credentials,
read-only candle/quote/context mounts, one CPU and 2 GiB memory. Output is a
separate report directory. `replay_driver.py` invokes the existing registered
strategy classes and scanner-evidence replay functions from the deployed image.

Bar study: all available **persisted-identity, covered canonical Delta bars**
through 2026-09-08 18:15 UTC. The usable 15m history starts 2026-09-01 00:00 UTC
and contains 741 BTC / 740 ETH rows. HTF price-only context uses the same official
cache plus validated canonical overlay as production; no official volume is
claimed as exact trade volume. Each decision uses only eligible closed context.

Quote study: **2026-09-04 12:00–16:00 UTC**, fixed before inspecting outcomes.
This is the session window on the last complete captured quote day. Range,
session and BoS each read their own lane-consumed BTC/ETH sequence. Quote shards
are pruned by timestamp metadata; rows are loaded in bounded batches. No candle
close or Binance ticker substitutes for Delta BBO.

Both studies explicitly lack shared-account/gateway approval parity and funding
history. A research acceptance or virtual fill is not a ManagedOrder or an
operational fill. Old-runtime quotes replayed with corrected logic cannot by
themselves prove old-versus-new live parity.

The initial raw-Parquet adapter attempt failed on legacy null identity/boolean
fields. Those errors were not counted as zero trades. The completed run uses
production's `_canonical_candle_frame` persisted-identity/coverage filter; absent
identity is never synthesized. The first-attempt artifacts remain on the VM at
`data/reports/release_86e8a65`; corrected artifacts are at
`data/reports/release_86e8a65_validated`.

## Bar results

| Strategy / clock | BTC | ETH | Interpretation |
|---|---|---|---|
| HTF continuation V2 / next 15m open | 516 evaluations, 0 trades | 515 evaluations, 0 trades | Regime/family permission denied; not missing EMA200 |
| Range observer V4 / next 15m open | Not testable | Not testable | Needs 2,017 warmup bars; fewer than 742 supplied |
| Trend-squeeze 1h / next open | 1 simulated trade, −46.79 bps net | 0 trades | Insufficient sample; no demonstrated edge |

The 1h study has 183 BTC / 182 ETH bars and 71 / 70 post-warmup evaluations.
The single BTC trade is not a portfolio return or a promotion verdict. Bars
with no signal do not earn a zero-return performance score.

## Quote results

See `summary.json` and the per-lane JSON artifacts for exact timestamps,
sequence input counts, decisions, cost profiles and outcomes. Range RT needs
2,017 15m warmup bars, so a large quote count cannot make that lane testable on
the shorter window. Session RT and BoS RT require 111 and 224 bars respectively.

BTC session RT produced one research short, entered at 15:01:10.833 UTC on
2026-09-04 and exited at 15:15 UTC as a failed breakout: gross −47.65 bps,
registered `delta_swing` execution estimate 15.8 bps, net **−63.45 bps**.
The quote engine's default research approval is not the full gateway approval.

| Quote mechanism | BTC | ETH | Qualification |
|---|---|---|---|
| Range expansion RT V2 | 0 intents | 0 intents | Insufficient structure warmup; NOT a successful parity test |
| Session continuation RT V2 | 1 research intent / outcome, −63.45 bps net | 0 intents | Four-hour mechanism test, not shared-account approval parity |
| Structure BoS RT V2 | 0 intents | 0 intents | A zero-versus-zero run is not positive parity evidence |

All 12 corrected runs completed with no execution errors. The isolated
container exited 0 at 2026-09-08 18:41:38 UTC, with no OOM. Six quote runs
processed **833,240 lane quote rows**; these include duplicates and repeated
observations across lanes and are not 833,240 unique venue events. Neither
positive approval parity nor profitable scalping was established.

## Limitations and decision

- No dedicated Delta squeeze-v4 or HTF quote-continuation lane tape was found.
  Raw recorder books are a different evidence scope, not substitute lane parity.
- Squeeze-v4 needs 2,065 closed 5m warmup bars. Its 5m clock, 48-bar maximum hold
  and costs must not be silently replaced with a 15m session or hourly result.
- The range studies are warmup failures, not evidence against the strategy's
  payoff. Historical bars without persisted identity are not upgraded to make
  those tests pass.
- Funding and full shared-risk/kernel execution were not replayed. None of
  these outputs is promotion-eligible, regardless of virtual profit.
- The backtest does not justify enabling scalping or weakening CostGate.

## Verification

- Full correctness suite for this code: 2,802 passed, 6 skipped.
- Focused acceptance, replay/parity, fee-model and kernel regressions after
  deployment: 76 passed.
- Changed-file lint and whitespace checks passed before release.

Machine-readable evidence and the driver are retained alongside this report.
