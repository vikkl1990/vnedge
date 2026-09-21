# BTC/ETH archive scanner screen — 18 September 2026

## Conclusion

Built and ran a separate, fixed-rule research screen. **No tested setup supports
promotion.** Do not loosen the live scanner gates based on this experiment.
This does not prove all pullback/reversal/breakout strategies fail. It rejects
these particular definitions, holding period and modeled execution assumptions.

113,131,407 trade rows from all 17 supplied archives were processed. The run
contains 96 result cells: two symbols, three setup families, four filter variants
and four time periods. The same data informs related comparisons; these are not
96 independent experiments. No parameters were changed after seeing outcomes.

## August historical holdout, baseline

Each event enters at the next 5m open and exits 15 minutes later. Mean returns
are basis points per event, not dollars or account returns.

| Symbol | Setup | Measured events | Gross | Modeled net at 17.8bps |
|---|---|---:|---:|---:|
| BTC | EMA pullback recovery | 475 | +1.11 | -16.69 |
| BTC | Prior-range reclaim | 358 | +0.17 | -17.63 |
| BTC | Compression breakout | 161 | -0.98 | -18.78 |
| ETH | EMA pullback recovery | 481 | -1.86 | -19.66 |
| ETH | Prior-range reclaim | 291 | +0.30 | -17.50 |
| ETH | Compression breakout | 184 | -0.35 | -18.15 |

All six regime-filtered August results were also negative. No August result
became positive at the lowest 11.8bps cost assumption. ETH regime-filtered
compression was less negative (-13.37bps, 45 events), but its descriptive daily
bootstrap interval was wide [-25.24, +4.05]bps. This is not a demonstrated edge.

Session qualification used January–May only, with at least 30 events, ten active
days and positive mean modeled return per symbol/family/side/six-hour block.
**No session qualified.** Session and combined variants therefore abstained.
Their zero counts are not evidence of a broken scanner.

## Coverage and interpretation

Across January 1–September 1 inclusive (70,272 possible 5m slots per symbol):

| Symbol | Five observed minutes | Missing or partial slots |
|---|---:|---:|
| BTC | 67,823 | 2,449 |
| ETH | 59,163 | 11,109 |

These are observed-minute checks, NOT proof that all trades were captured.
Missing slots remain missing. Features restart and outcomes crossing a missing
slot or split boundary are censored. The archives lack IDs/completeness manifests;
UTC timestamp interpretation and contract-to-base conversion remain explicit
research assumptions. ETH March is absent. July ends on the 26th; September
contains only the 1st. These files do not repair September 12–17 live gaps.

Funding is absent. Costs are frozen assumptions (11.8, 17.8, 23.8bps round trip),
not measured bid/ask execution. This screen has no stops/targets, lot sizing,
gateway, order manager, risk brakes or fill reconciliation. It is an event-return
screen, not a complete paper backtest. Historical holdout is exploratory, not a
claim that these markets were previously untouched. Bootstrap intervals are
descriptive and not adjusted for multiple comparisons.

## What was built

- Chunked archive ingestion with identity/value/order validation and file hashes.
- Observed-minute aggregation, gap-aware 5m features and causal setup detectors.
- Baseline, regime, training-only session and combined comparisons.
- Next-open measurement, overlap reservation, censoring and cost sensitivities.
- Per-event outputs, coverage audit, input/code/contract fingerprints and summary.

Existing registered scanners, execution paths and trading permissions are unchanged.

Validation: full repository suite **3,382 passed, 6 skipped**. The expanded
archive-specific suite **12 passed** (seven additional boundary tests were added
after the full-suite run started). Input count, all 96 result cells, code and
contract hashes, and disabled authority flags were independently checked.

## Reproduce

Run from the repository root, using a new output directory (existing output is
refused rather than overwritten):

```sh
.venv/bin/python -m vnedge.research.archive_scanner_screen \
  --downloads /Users/scorpion/Downloads \
  --output research/archive_scanner_20260918/attempt_02 \
  --contract research/archive_scanner_20260918/CONTRACT.md
```

Attempt 01: `attempt_01/report.json`; event files and research-only Parquet bars
are alongside it. Frozen contract SHA256:
`c5d003048bee12bb4c1972f0cd908079ca07515d0d3d4c878d8813df33c1cc38`.

## Next decision

Do not attach these filters to live HTF v2. First establish whether a separately
preregistered longer-horizon or different-mechanism candidate has sufficient
gross follow-through to cover realistic costs. Any subsequent search on this
history remains exploratory and needs fresh future evidence before promotion.
