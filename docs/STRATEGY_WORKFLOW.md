# VNEDGE strategy workflow

VNEDGE now has a versioned, lineage-aware research workflow inspired by the
useful governance mechanics visible on Trader.dev's public strategy browser and
backtest reports. It does **not** copy private strategy code and it does not
adopt leaderboard-first promotion.

Sources reviewed:

- <https://mcp-api.trader.dev/browse> — browse/filter/sort/version/fork surface
- <https://mcp-api.trader.dev/login> — build, backtest, optimize, alert workflow
- <https://mcp-api.trader.dev/pricing> — version comparison and sweep workflow

## Adapted workflow

```text
idea
  -> reviewed strategy_id in strategy_registry.py
  -> immutable revision snapshot (code hash + config hash + engine identity)
  -> causal backtest report
  -> engine-parity replay
  -> pre-registered untouched OOS judgment
  -> optional SHADOW_OBSERVE allowlist review
  -> paper evidence
  -> human promotion review

parity fail / implausible result / contract drift
  -> quarantine this revision
  -> fork to a NEW reviewed strategy_id
  -> restart evidence for the new mechanism
```

The workflow is intentionally stricter than a public leaderboard:

| Public-platform mechanic | VNEDGE adaptation |
|---|---|
| Versioned strategy | Immutable revision with config/code fingerprint |
| Fork | Parent lineage; behavioral changes require a new strategy ID |
| Backtest report | After-cost result plus trade count, drawdown, provenance |
| Engine identifier | Engine name/version plus explicit parity result |
| Leaderboard filters | Read-only UI filters; no best-result auto-promotion |
| Quarantine | Terminal for the affected revision; history is retained |
| Deploy/alert | Remains outside this workflow; registry and risk gates still rule |

## Storage contract

Canonical event ledger:

```text
research/strategy_workflow/registry.jsonl
```

It is append-only, `fsync`-persisted, and hash chained. Editing, deleting, or
reordering an event breaks verification. The file should be committed whenever
an explicit revision, fork, parity result, quarantine, or retirement is added.

The dashboard snapshot is generated at:

```text
research/live_research/strategy_workflow_latest.json
```

That snapshot joins:

- the workflow ledger;
- the reviewed Python strategy registry;
- rolling walk-forward results;
- the hash-chained untouched-data burn registry;
- paper-forward reports;
- current `RESEARCH_ONLY`, `SHADOW_OBSERVE`, and `KILLED` policy.

## Commands

Register the first frozen revision of an already-reviewed strategy:

```bash
.venv/bin/python -m vnedge.research.strategy_workflow register \
  --strategy range_expansion_observer_v3 \
  --version 1 \
  --mechanism "closed-bar range expansion" \
  --timeframe 1h \
  --symbol 'BTC/USDT:USDT' \
  --params '{"range_multiple":1.5,"max_hold_bars":12}' \
  --engine vnedge_event_backtester \
  --engine-version 1
```

Fork a revision. The child must already have its own reviewed strategy ID:

```bash
.venv/bin/python -m vnedge.research.strategy_workflow fork \
  --parent 'range_expansion_observer_v3@1+CONFIG_HASH' \
  --strategy range_expansion_observer_v4 \
  --version 1 \
  --params '{"range_multiple":1.8,"max_hold_bars":12}'
```

Record deterministic engine parity:

```bash
.venv/bin/python -m vnedge.research.strategy_workflow parity \
  --revision 'range_expansion_observer_v3@1+CONFIG_HASH' \
  --status PASS \
  --reference-run old-engine-run \
  --current-run current-engine-run \
  --max-delta 0
```

A `FAIL` parity result requires a reason and automatically quarantines that
revision. It does not quarantine siblings or descendants.

Build the read-only dashboard artifact and verify the event chain:

```bash
.venv/bin/python -m vnedge.research.strategy_workflow build
.venv/bin/python -m vnedge.research.strategy_workflow verify
```

## Dashboard

The Research tab reads `GET /strategy-workflow` and shows:

- strategy/version and fork parent;
- evidence stage;
- latest after-cost result, profit factor, and trade count;
- engine parity and frozen engine identity;
- governance gaps such as missing OOS, under-sampling, or quarantine.

Filters are read-only. There are deliberately no dashboard buttons to fork,
promote, arm, or trade.

## Safety boundary

This workflow is evidence management, not authority. Every payload hard-codes
`can_trade=false` and `can_promote=false`. Existing strategy registry,
untouched-window judgment, shadow allowlist, human gate, and
`PreTradeRiskGateway.evaluate()` contracts remain unchanged.
