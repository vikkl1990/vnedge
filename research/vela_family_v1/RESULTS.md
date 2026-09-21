# VELA family v1: literal-spec replay results

## Outcome

The one-position A / gated B behavior reproduces. The claimed long-window
profitability does not reproduce on this dataset and date interval. No strategy
is activated or promoted. This is research code, not a production state store,
dashboard or paper/live order adapter.

Source: Delta India public BTCUSD daily and 15m candles, downloaded through the
[documented historical candle endpoint](https://docs.delta.exchange/#get-historical-ohlc-candles).
This is venue OHLC research evidence, NOT canonical trade-lake evidence.
Full evaluation: **2025-01-01 through 2026-09-19 UTC** (end exclusive Sep20).
Daily warmup begins2024-11-01. All expected daily/15m timestamps were present.
API payload receipts, URLs, retrieval timestamps and hashes are in
`attempt_01/source/`. Code/contract hashes and all36 summaries: `attempt_01/report.json`.

The original “21-month” dates and dataset are unspecified, so differences from
quoted PFs do not establish a bug in the original calculation. This report
states precisely what was replayed instead of fitting those claims.

## A alone — close fill, $1,000 notional clips

| Prior range | Full closed trades | Full net USD | Full PF | Last90d trades / PF | Last14d trades / PF |
| --- | ---: | ---: | ---: | ---: | ---: |
| Yesterday | 112 | -111.18 | 0.929 | 12 / 1.138 | 4 / 0.631 |
| 10 days | 48 | -495.15 | 0.580 | 4 / 0.801 | 1 / 0.000 |
| 20 days | 22 | -210.92 | 0.703 | 2 / 2.272 | 1 / 0.000 |

Each window starts FLAT independently, with pre-window indicator warmup.
Last90d is Jun22–Sep19; last14d is Sep6–Sep19. These are not slices of a
single inherited portfolio. The 20d 90-day PF comes from just **two closed
trades**, plus an open position; it does not demonstrate a durable edge.

Next-open full-run PFs: yesterday0.930;10d0.580;20d0.703. Changing fill timing
alone does not rescue the full-window results on these candles.

## B's incremental contribution in the full run

B is permitted only when A is FLAT at07:00. The table is B's own closed-trade
result, not pooled with A. A's trades are unchanged when B is enabled.

| A range | B confirmation | B trades | B net USD | B PF |
| --- | --- | ---: | ---: | ---: |
| Yesterday | off | 248 | -203.37 | 0.743 |
| Yesterday | candle_proxy | 168 | -94.87 | 0.821 |
| 10d | off | 333 | -383.06 | 0.671 |
| 10d | candle_proxy | 214 | -220.17 | 0.709 |
| 20d | off | 302 | -420.58 | 0.617 |
| 20d | candle_proxy | 194 | -201.94 | 0.704 |

No real CVD was available or fabricated. The CVD-missing rejection path is
unit-tested; no CVD performance run is represented as evidence.

## Claimed occupancy — confirmed, with timestamps clarified

In the continuous full-window close-fill run, all three A range modes:

- Signal on the **Aug21 daily bar**, which closes **Aug22 00:00 UTC**.
- Enter LONG at78,317.50 and remain open through the Sep19 close.
- Suppress B throughout Sep6–Sep19: **zero B trades** in that interval.
- Return MANAGE on the Sep18 bar; no duplicate A long.

If evaluated FLAT on Sep18, yesterday and10d produce SIGNAL LONG;20d WAIT.
Independent 14-day runs start without the August position, so B trades there
are expected and do not contradict the continuous-book claim.

Open full-run A levels at end:

| Range | Entry | Stop | Target | Break line |
| --- | ---: | ---: | ---: | ---: |
| Yesterday | 78,317.50 | 68,898.00 | 82,804.50 | 73,385.00 |
| 10d | 78,317.50 | 62,505.00 | 89,197.50 | 73,385.00 |
| 20d | 78,317.50 | 62,245.00 | 89,457.50 | 73,385.00 |

These are historical simulated positions, not actual holdings or current
trading instructions. They were not force-closed at the test boundary.

## Accounting and execution limitations

Fees .00059 of entry and exit notional; slippage$.50/BTC per fill deducted as
cash. Fractional BTC qty=1000/entry, no compounding. Stops/targets filled at
the specified levels; stop wins ambiguous bars. The model does not worsen
stops for gaps beyond the stop, per the supplied literal rules. Close fills
also assume execution at a price only known when that bar closes. These are
optimistic execution simplifications, not venue fill evidence.

Funding, exchange lot rounding, spread/latency/queue modeling and risk gateway
are excluded. PF and net USD are **closed-trade** statistics. Open-position
P&L and its entry costs are not included in those closed-trade totals; the open
inventory is explicitly retained in each result. Drawdown is realized closed-
trade cash drawdown, not mark-to-market account drawdown. Do not interpret these
USD results as returns on a continuously invested $1,000 account.

## Test and artifact scope

16 focused tests cover causality, range modes, partial bars, strict boundaries,
entry-bar exclusion, stop-first, kill-only-at-close, no same-bar re-entry,
pending-open validation/target, B box timing/EOD, missing CVD, proxy rejection,
occupancy timing, gaps and per-fill costs. All12 variants run over all3 windows.

Each `attempt_01/<window>_<range>_<fill>_<Bmode>.json` contains event history,
closed trades, open/pending/unresolved state and per-book metrics. The harness
does not force acceptance assertions on market-dependent PF or position claims.

Conclusion: retain VELA as research. Its state/occupancy design is coherent;
this replay does not support advertising daily,10d or20d as a proven edge.
