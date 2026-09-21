# SATS v1.13.1 test — 18 September 2026

## Verdict

**Do not promote these tested settings.** The source-style gross result is
approximately break-even on BTC and negative on ETH. Modeled execution costs
make every tested setting negative in both June–July and August.

This is a Python translation of the supplied indicator's default signal and
trade-accounting logic, not a successful TradingView compilation or verified
bar-for-bar Pine reproduction. Original author: WillyAlgoTrader. Attribution
does not imply author endorsement of the translation or results.

## Scope

Eight fixed settings: BTC/ETH × 5m/15m × Auto/Crypto 24/7. Four execution worlds
and four fixed historical periods give 128 result cells. Fixed targets at
1R/2R/3R, thirds, static stop, 100-bar timeout, opposite-trend exit, confirmed
3/3 pivots, default adaptive/TQI/character-flip logic. Auto-calibration and
dynamic TP are OFF, as supplied. No score-grade entry filter was added.

The test reuses the archive screen's derived bars from 113,131,407 trade rows.
It does not reuse that screen's entry logic or fixed 15-minute outcome measure:
SATS trades here follow their own partial targets, stops, flips and timeouts.

## August historical test

The main execution model enters on the next open, charges 5.9bps per fill and
3bps adverse market slippage, including proportional exit fees. TP touches fill
at their limit. Funding is unavailable and excluded. These are frozen modeling
assumptions, not verified historical account fees or measured executions.

| Symbol | TF | Preset | Closed trades | Mean modeled net R | PF (R) | Mean modeled net bps |
|---|---|---|---:|---:|---:|---:|
| BTC | 5m | Auto | 1,172 | -0.864 | 0.106 | -17.77 |
| BTC | 5m | Crypto 24/7 | 553 | -0.855 | 0.158 | -17.99 |
| BTC | 15m | Auto | 234 | -0.479 | 0.300 | -17.28 |
| BTC | 15m | Crypto 24/7 | 157 | -0.377 | 0.423 | -15.80 |
| ETH | 5m | Auto | 1,009 | -0.602 | 0.187 | -18.06 |
| ETH | 5m | Crypto 24/7 | 431 | -0.575 | 0.280 | -19.41 |
| ETH | 15m | Auto | 152 | -0.279 | 0.479 | -20.07 |
| ETH | 15m | Crypto 24/7 | 103 | -0.297 | 0.491 | -19.49 |

R is return relative to each trade's initial stop distance, not a dollar
portfolio return. Mean R and mean bps need not share the same sign because
stop distances differ. PF uses R, not unweighted dollars. Closed-trade drawdown
in the machine-readable report is additive R, NOT mark-to-market account DD.

For comparison, BTC15m Crypto's source-style zero-cost close-entry model showed
+0.072R and +1.24bps per trade (157 trades, PF1.188). That modest gross movement
does not cover the modeled bill. Increasing market slippage to6bps per leg
made all eight August settings negative as well. Execution changes can change
trade counts: delayed entries past TP1 are refused, and slipped stops can hit
before a reversal. This is not simply subtracting one cost from identical trades.

## Code findings

1. The file declares `indicator()`, not `strategy()`. It maintains its own
   simulated ledger. The built-in source defaults are zero commission and zero
   slippage. Performance labels are not broker-emulator fills.
2. Source entry is the signal close. A close-confirmed alert cannot guarantee
   that historical fill. The test reports this optimistic source-style case
   separately from the next-open adaptation. TradingView's own strategy
   documentation distinguishes order creation from next-tick execution:
   https://www.tradingview.com/pine-script-docs/concepts/strategies/
3. Entries and ledger updates require `barstate.isconfirmed`. No explicit
   `request.security`, lookahead, or `varip` was found. Pivots are used only
   after confirmation, not backdated into the entry. Bands can still fluctuate
   on the current forming bar; closed-signal checks are not a promise that all
   intrabar visuals are fixed. Execution semantics reference:
   https://www.tradingview.com/pine-script-docs/language/execution-model/
4. Grades and score components are display/optional alert conditions. Default
   entries accept all valid warmed-up flips; a low score does not block them.
5. Stop-first ambiguity, partial initial-risk accounting, worse-open stop-gap
   fills and no loss clipping are sensible safeguards in the source model.
   Limit touches still do not prove an executable fill or queue priority.
6. “Self-learning” is optional hill-climbing of one parameter, disabled by
   default. This run does not evaluate calibration-on, dynamic TP or custom
   source inputs, and does not claim trained machine-learning performance.

## Limitations and safeguards

- Missing archive slots reset indicator state. Trades/pending entries crossing
  gaps are censored/cancelled, not carried through invented prints. August
  censored positions per setting are BTC5m2/BTC15m1, ETH5m7–8/ETH15m5.
  Censored trades, including their partial realized results, are excluded from
  closed-trade metrics and remain visible in event records. This can bias the
  measured sample; it is not a complete-history profit claim.
- UTC, contract conversions and historical tick sizes remain explicit research
  assumptions. Present minute buckets do not certify trade completeness.
- The Python port assumes rightmost-equal pivot semantics. No TradingView
  signal export was available to establish exact parity, including ties/seeding.
- No funding, measured spreads, lots, sizing, gateway, portfolio risk brakes or
  reconciliation. This is not a full VNEDGE paper-order replay.
- August is already-seen history, not untouched OOS. No parameters were tuned
  from these results. Positive isolated gross figures do not establish an edge.

## Reproduce and inspect

Validation: **3,404 repository tests passed, 6 skipped**, including **15 SATS
tests** covering causality, pivot delay, gap resets, partial fees, stop-first
ambiguity, stop gaps, delayed entry and reversal ordering. Code/contract hashes
and all128 result cells were checked after replay. These tests verify the
translation's stated behavior, not native Pine parity.

`attempt_01/report.json` contains all128 cells, source/code/contract/data hashes,
entry/censor counters and exit reasons. Adjacent JSONL files contain trade
records. Existing output directories are refused rather than overwritten.

```sh
.venv/bin/python -m vnedge.research.sats_screen \
  --parent research/archive_scanner_20260918/attempt_01 \
  --source /Users/scorpion/.codex/attachments/8758437b-4d40-4863-9ffe-8acbfe069c37/pasted-text.txt \
  --contract research/sats_20260918/CONTRACT.md \
  --output research/sats_20260918/attempt_02
```

No scanner registration, live permission, VM deployment or order submission
was changed. A TradingView export matching the exact bars/settings is the next
step for parity verification; a cost-sensitive gross result alone is not a
reason to integrate this with the live bot.
