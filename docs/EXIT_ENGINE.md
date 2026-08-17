# Canonical exit engine

`vnedge.runtime.active_exit` is the only promotion-eligible exit decision
contract.

## Contract

`SignalIntent` is converted to an `ExitEngine` containing:

- `ActiveExitState`: mutable stop, MFE, breakeven, and TP-ladder state;
- `ExitEngineConfig`: frozen trail, holding-cap, tick-stop, partial-TP, and
  fee-aware-breakeven semantics;
- pure `on_bar`, `on_tick`, and `on_strategy_exit` decision methods;
- `mark_fill`, the only operation allowed to advance a partial TP ladder.

Backtest, replay paper, live paper, and guarded live sessions call this façade.
They may differ in fill prices and venue adapters, but not in stop/TP/trail
decision math. Stop wins when stop and target cross in the same bar. ATR trails
tighten after a non-exit bar and therefore apply only to later bars.

## Live partial-exit boundary

Live partial TP is disabled until order-manager, fill-ledger, reconciliation,
and restart tests prove partial-fill state. A guarded live session rejects
`allow_partial_tp=True`; with the supported full-only policy, a ladder trigger
becomes a full reduce-only close while retaining the observed TP reason.

Paper and backtest may use `allow_partial_tp=True` because their partial fill
accounting is deterministic and tested.

## Restart continuity

`ActiveExitState.to_dict()` persists entry/quantity, the active stop,
breakeven and ladder progress, MFE/history, and the entry-time trail and
fee-aware-breakeven parameters. On restore, the stop may tighten relative to
the original signal stop but can never loosen, ladder progress is bounded to
the registered targets, and duplicate TP fill acknowledgements are
idempotent.

## Promotion boundary

- Promotion evidence must use `BacktestConfig(promotion_contract=True)`, which
  requires the active exit path.
- `backtest.trailing_exit` is a separate tick-research simulator and reports
  `promotion_eligible=false`.
- A tick-trailing hypothesis must first be expressed as `ExitEngineConfig` and
  re-run out of sample before it can be considered for promotion.

## Submission boundary

The engine never submits orders. Each runtime converts `ActiveExitDecision` to
a reduce-only order through its normal risk gateway, journal, idempotency, and
reconciliation path. Plans clear only after an accepted/final exit or confirmed
flat venue state.
