# Session/regime scoring — first replay result

**Verdict: no suitable scoring logic demonstrated. Keep operational scanners
unchanged.** The experiment supports building a better evidence vocabulary; it
does not support switching on a22-indicator score or claiming an edge.

## What actually ran

- Five NEW research detectors: range breakout, wick reversal, EMA reclaim, BOS
  and CHOCH. These are not the deployed scanner modules or the HTF-v2 telescope.
- Sixteen requested families have explicit limited representations; six remain
  deferred. See [all22 mappings and research sources](RESEARCH.md).
- Five capped groups: structure, location, momentum, candle, participation.
- Baseline, balanced60, regime60, training-only session/regime permission;
  five drop-one-group diagnostic ablations. All results retained.
- DeltaBTC/ETH, closed5m decisions, stored-provenance validated1m outcome path.
  UTC Sep1–6 training; Sep6–11 test with20-minute embargo. The entire ten-day
  window was previously researched: **NOT untouched OOS**.
- Entry proxy one minute after close, fixed15-minute response. This is a
  price-response backtest, **not** stop/target, BBO acceptance, risk, account or
  kernel execution. No market fills or operational ML labels are manufactured.
- Fixed `delta_scalp_v2`:17.8bps modeled round trip once, funding excluded.
  Fee-only11.8 and stress23.8 on the same events. No fee waiver or reduced wall.

## Test-period results

Each entry is **mean net bps (measured event count)**. “—” is unmeasured, not zero
return. Counts below30 are insufficient even before accounting for selection,
dependence and short history. These are separate experiments, not one portfolio.

| Symbol / detector | Unscored baseline | Balanced60 | Regime60 |
|---|---:|---:|---:|
| BTC breakout | −17.56 (73) | −17.10 (29) | −14.02 (10) |
| ETH breakout | −18.73 (68) | −11.71 (23) | −16.52 (7) |
| BTC wick reversal | −18.56 (44) | — (0) | — (0) |
| ETH wick reversal | −18.44 (41) | — (0) | — (0) |
| BTC EMA reclaim | −18.15 (87) | −20.09 (12) | −10.12 (5) |
| ETH EMA reclaim | −19.05 (78) | −10.31 (2) | −19.42 (4) |
| BTC BOS | −19.23 (24) | −17.55 (19) | −19.74 (4) |
| ETH BOS | −12.61 (21) | −13.98 (13) | −23.96 (4) |
| BTC CHOCH | −12.63 (21) | — (0) | +31.94 (1) |
| ETH CHOCH | −12.01 (23) | — (0) | −23.43 (1) |

The single positive BTCCHOCH event is not a lead with usable support. Across
all90 symbol/family/variant test cells, **none with >=30 outcomes has positive
mean net**. The positive diagnostic-ablation cells have just1–4 outcomes. Do
not choose those windows/features as the next “winner.”

### Session conditioning

Training-only cell=(symbol,detector,six-hourUTCblock,hourlyregime), based on
regime60 scheduled events. Only32 cells were populated; the largest contained
**four measured events**. None met the predeclared30-outcome/three-day minimum.
Therefore session_regime60 selected **zero** test events for every detector.
This is **insufficient training support**, not zero PnL, proven filtering or a
model that learned when to trade. No session-specific weights were learned.

## What went wrong / what this teaches

1. **Score is not edge.** ETH balanced breakouts improved gross mean from
   −0.93 to+6.09bps, but still lost11.71bps after modeled costs. Its23-event
   sample is also insufficient. BTC EMA reclaim deteriorated with the score.
2. **Continuation and reversal cannot share an unquestioned score.** Rewarding
   existing direction-aligned structure/momentum suppresses reversal/CHOCH.
   The zero selections do not disprove their underlying claims. A family-specific
   scoring revision must be frozen separately, not patched into this run.
3. **Context fragments evidence.** Even ten days of fairly dense bars yields
   tiny session/regime cells after setup and score selection. More score
   dimensions do not create more independent observations.
4. **Scalping costs remain material.** Most unfiltered gross responses are
   approximately flat. Strong-looking pattern agreement does not make the next
   fifteen-minute move large enough. This says nothing definitive about another
   exit horizon or maker execution; those would be separate contracts.
5. **Some inputs overlap or are proxies.** FVG/levels/lines are OHLC geometry;
   they are not measured resting liquidity. Three momentum transforms are not
   three independent confirmations. Group caps reduce, but do not eliminate,
   correlation or missingness bias.

## Coverage and verification

| Input | BTC | ETH |
|---|---:|---:|
| Eligible stored5m / scheduled2880 | 2864 | 2863 |
| Eligible1m / scheduled14400 | 14378 | 14377 |
| Bad/missing5m calendar slots | 16 | 17 |
| Warmup/gap-ineligible5m slots | 406 | 455 |
| Generated detector events, before independent book scheduling | 862 | 783 |
| Events with unknown hourly context | 238 | 300 |

Gaps remain in the calendar and reset indicator/swing state. Unknown context
blocks contextual variants, not the context-free baseline. Censored future
paths reserve their full interval; they are never silently counted as zero.

Independent verifier checked input/source hashes, chronology,17.8bps arithmetic,
actual minute proxies, no overlapping positions per experiment, censored paths,
phase separation, trained cells, mean/PF/drawdown and safety flags:
**170 train/test books,2530 scheduled event records verified**. Records repeat
across variants and are **not2530 independent trades**.

Reporting correction: the reused summary helper's bootstrap samples all ten
calendar days even when called for one five-day split. Its raw confidence
interval field is marked **DO_NOT_USE** in verification.json. The verifier
supplies phase-specific five-day descriptive intervals, null for sparse samples.
Raw outcomes were preserved; no signals, thresholds or net results changed.
Neither interval is a multiple-testing-adjusted significance claim.

Tests: **10 new causality/identity/scoring tests passed**; full existing suite
**3077 passed,6 skipped**. Software tests do not establish economic edge.

## Recommended next work — not automatic deployment

Finish ML Lab's reconciled outcome binding. Retain these features as research
measurements with exact versions and timestamps, not fake training labels.
Then preregister at most one continuation-specific and one range-reversal-specific
score, gather materially longer verified histories across several market states,
and compare against their unscored baselines on genuinely untouched forward data.
Use abstention when context cells are unsupported. Do not wire these experimental
weights into live IDs, loosen cost/risk gates, or promise that more data will
necessarily reveal an edge.

## Artifacts / reproduction

- [Frozen experiment](CONTRACT.md)
- [Research rationale and all22 families](RESEARCH.md)
- [Raw results, manifests, all ablations and training cells](attempt_01/results.json)
- [Independent verification and corrected descriptive intervals](attempt_01/verification.json)
- `attempt_01/*.features.jsonl`: candidate-time evidence.
- `attempt_01/<symbol>.<family>.<variant>.<phase>.jsonl`: scheduled outcomes.
- Runner: `.venv/bin/python research/reports/context_scoring_20260913/screen.py --input /tmp/vnedge-htf-recheck.PInWIi`
- Outputs refuse overwrite. Do not rerun to tune this already-seen window.
- Verifier: `.venv/bin/python research/reports/context_scoring_20260913/verify.py`

All work is local research. No VM deployment, orders, roster, registry, cost
profile or capital changes. `can_trade=false`, `can_promote=false`,
`performance_eligible=false` throughout.
