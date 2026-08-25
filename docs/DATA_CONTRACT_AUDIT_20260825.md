# Runtime and UI data-contract audit — 2026-08-25

Scope: active scanner counters, shadow valuation, execution costs, lane state,
and operator-facing labels. This audit changes observability and cost assumptions;
it does not grant order authority or alter the promotion ladder.

## Corrected conflicts

| Conflict | Canonical contract after this change |
|---|---|
| Quote scanner accepted a setup while the funnel showed zero signals | Every quote candidate increments `signals` and `live_signals`; approvals/rejections remain separate counters. |
| Accepted quote entry was invisible in the lane blotter | `last_quote_signal` publishes side, price, stop, approval, and path; open shadow intent is displayed as a position, never as an exchange order. |
| Shadow net contained only closed trades | Closed net remains `resolved_net_usd`; executable bid/ask mark-to-market is `open_unrealized_net_usd`; `total_net_usd` is their explicit sum. |
| Long and short open values could use a non-executable mid | Longs mark at bid; shorts mark at ask. Full booked round-trip cost is deducted from the open estimate. |
| Binance public data silently selected Binance cost assumptions | `data_exchange` and `execution_cost_exchange` are separate fields. The production observer roster explicitly models `delta_india` costs. |
| Runtime CostGate assumed 2R while the HTF arm used 2.5R | One resolver reads the frozen scanner payoff hypothesis. The value is identified as stop-distance geometry, not empirical expectancy. |
| Heartbeat said `waiting` while a shadow scanner position was open | Runner state and no-trade reason now report the open shadow position and exit-management state. |

## Deliberately unchanged

- `net_usd` remains a compatibility alias for resolved/closed shadow PnL.
- Open mark-to-market is display-only and cannot affect sizing, exits, risk, or
  promotion evidence.
- CostGate still uses a conservative deterministic fee profile.
- All active scanners remain `SHADOW_OBSERVE`; capital approval remains empty.
- A payoff hypothesis is not expected value. Promotion still requires sealed,
  after-cost evidence.

## Verification

- Full suite: `2334 passed, 6 skipped`.
- Focused runtime/UI suite after all changes: `149 passed` (run at handoff).
- Ruff on changed production files and focused tests: clean.
- Existing repository-wide mypy debt remains outside this change, including
  missing pandas stubs and pre-existing optional-state findings in `live_paper.py`.
