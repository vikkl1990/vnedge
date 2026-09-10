# Signal drought: verified state and narrow corrections

## Observed on deployed abc1fc9

Read-only checks on 2026-09-10 around 14:57 UTC, using the 14:45 live
evaluation and the bound context/lake (not just the startup candle cache):

- BTC and ETH: data-ready and decision-ready, valid canonical decision hashes,
  800 bound daily observations and EMA200 ready. The earlier HTF refresh fix
  has survived subsequent live 4h/day boundaries.
- Permission remains `mean_revert / weekly_range_macd_off`. That is a valid
  continuation denial, not a warmup failure.
- Structure has a valid last closed 1h parent, but only one retained confirmed
  high and zero retained confirmed lows since the last quality reset. The
  confirmed high was visible at 07:00 UTC on BTC and 10:00 UTC on ETH.
- Delta disconnects at 21:08, 22:47 and 23:06 UTC on September 9 correspond
  to incomplete buckets. No later websocket disconnect appeared in the
  inspected log interval. This is not proof of perfect trade completeness:
  late prints are still rejected by the immutable watermark.
- Binance's separate owner repeatedly aborts maintenance on a September 8
  16:35 UTC immutable 1m repair conflict. It is not the Delta regime blocker.

## Changes in this slice

1. Startup publishes `evaluation_status=awaiting_first_evaluation` and
   `ema200_ready=null`. Loaded daily count remains visible. Readiness stays
   false until evaluation; a genuinely short daily frame is still reported.
2. `/ready` and Desk retain that distinction. Unknown EMA evidence is not a
   failed EMA calculation and is not permission to trade.
3. BoS carries diagnostic-only retained high/low counts (each capped at two),
   latest confirmation times, last ineligible-parent reset time, eligible
   rows since reset, and a reason through the same closed-parent as-of join.
   Missing parent identity wipes these joined fields; it cannot display the
   previous parent's swing counts as current.
4. HTF v2 no longer discards `structure_parent_missing` in its diagnostics.
   A populated-but-NaN structure-ready diagnostic cannot count as true.
5. Binance owner maintenance retries back off from the configured retry
   interval to the normal tail interval. Success resets backoff. Immutable
   conflicts still abort before writes; no health gate is cleared by waiting.

No swing detection, gap reset, regime threshold, scanner roster, order path,
cost profile or capital permission changed. Diagnostic columns are not gates.
Eligible-row count is not proof of consecutive hours when a parent is absent.

## Remaining evidence, not shortcuts

- A broken connection's missing public trades cannot be reconstructed from
  OHLC or a dense-looking raw shard. Keep those buckets partial unless an
  independently verifiable complete tape exists.
- The 800 official daily context bars are not 800 canonical lake days.
  Arena's 2,160-hour canonical-history requirement remains separate.
- Binance's conflicting repair needs a comparison of the staged and existing
  row provenance; bounded retries do not resolve the conflict itself.
- New confirmed swings do not guarantee a signal while the permission state
  denies continuation. Quote approval parity, economics and live gates remain
  separate requirements.

Validation should cover startup -> first evaluation, stale parent masking,
gap resets without stale anchor reuse, prefix-causal diagnostics, unchanged
existing signal fixtures, and retry backoff/reset. Deployment is a separate
step; do not restart the recorder merely to refresh the UI.
