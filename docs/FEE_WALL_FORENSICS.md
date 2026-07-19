# Fee-Wall Forensics

VNEDGE scalper research must separate three different failures that used to
look identical in the reports:

1. Price never moved enough to pay route cost.
2. Price moved enough, but the exit gave the move back.
3. The command did not see the candle dataset, so no strategy verdict exists.

The execution edge labeler and router now publish explicit trade-geometry
fields for every labeled event:

- `hold_bars`: bars held from next-open entry to stop, target, or horizon.
- `time_to_mfe_bars`: bars needed to reach maximum favorable excursion.
- `mfe_after_cost_bps`: best favorable move minus maker/taker route cost.
- `capture_ratio`: realized gross bps divided by MFE bps.
- `fee_wall_break_rate_pct`: percentage of routed events whose MFE cleared
  fees before exit.
- `exit_diagnosis`: `MOVE_NEVER_CLEARED_COST`,
  `GAVE_BACK_FEE_WALL_MOVE`, `STOP_BEFORE_TARGET`,
  `HORIZON_EXIT_FAILED_CAPTURE`, or `CAPTURED_AFTER_COST`.

Use this to tune scalpers as chart systems, not only as pass/fail scanners:

```bash
python -m vnedge.research.execution_edge_router \
  --exchange binanceusdm \
  --symbol ETHUSDT \
  --timeframe 15m \
  --lookback-days 30 \
  --horizon-bars 8 \
  --min-edge-bps 8 \
  --min-profit-factor 1.15
```

Interpretation:

- `MOVE_NEVER_CLEARED_COST`: entry thesis is too weak for the venue/route.
  More exit tuning will not rescue it.
- `GAVE_BACK_FEE_WALL_MOVE`: the signal found movement, but target/trail/horizon
  is wrong. This is the best candidate for sniper-target work.
- `STOP_BEFORE_TARGET`: stop geometry is too tight or entry is late.
- `HORIZON_EXIT_FAILED_CAPTURE`: the holding window missed the move timing.
- `CAPTURED_AFTER_COST`: this is what a promoted scanner must concentrate.

If the CLI reports a missing candle lane, treat that as a data/config problem,
not a market verdict. Fix the downloader, symbol alias, or container volume
mount before judging edge.
