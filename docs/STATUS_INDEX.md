# Documentation status and precedence

Status as of 2026-08-16. Documentation describes evidence and operation; it
never grants strategy or capital permission. Code policy in
`strategy_registry.py`, `hf_engine_registry.py`, the runtime gates, and the
pre-trade gateway takes precedence over every document.

## Active operational documents

These describe the current measurement-first system:

- `DESIGN.md`, `ARCHITECTURE.md`, `ARCHITECTURE_FLOW.md`
- `DEPLOY.md`, `RUNBOOKS.md`, `DASHBOARD_AUTH.md`, `GO_LIVE_GATE.md`
- `PROTECTIONS.md`, `EXECUTOR_RUNTIME.md`, `LIVE_LADDER.md`
- `CANDLE_PIPELINE.md`, `MARKET_PULSE.md`, `CONTEXT_DATA_BACKFILL.md`
- `AGENT_GATEWAY.md`, `RESEARCH_AGENTS.md`, `AI_SANDBOX.md`
- `strategy_contract.md`, `PROMOTION_REVIEW_RUNBOOK.md`
- `EXIT_ENGINE.md`

“Active” means the document describes a supported component. It does not mean
any strategy is capital-approved. `CAPITAL_APPROVED` is currently empty and
Delta live remains blocked pending a native private order/fill stream.

## Governance and research-only documents

Documents concerning alpha factories, external signals, forecasts, mining,
agent councils, backtests, strategy labs, uplift, indicators, or candidate
strategies are research-only. This includes filenames containing:

```text
ALPHA  SIGNAL  STRATEGY  RESEARCH  FORECAST  BACKTEST  UPLIFT
MINER  REVERSION  BREAKOUT  SCALP  LEADLAG  REGIME  FACTORY
PINE  FREQTRADE  LUXALGO  OCTOBOT  IAF  GITHUB  PUBLIC_BOT
```

They may explain hypotheses or historical results but cannot populate a paper
or live roster. Promotion requires a reviewed code change adding the exact ID
to `CAPITAL_APPROVED` with pre-registered OOS and paper evidence.

`PATH_HOLD_POLICY.md` and its pure policy module are also explicitly
research-only and are not wired into a registered strategy.

## Historical and post-mortem documents

Files whose names contain `AUDIT`, `POSTMORTEM`, `VERDICT`, `DRIFT`, or a dated
investigation suffix are immutable evidence snapshots. Their recommendations
may have been superseded by later code. In particular:

- `EDGE_INVESTIGATION_POSTMORTEM_20260816.md` records the fast-edge and Funding
  MR kill decisions.
- `E2E_AUDIT_20260813.md`, `CODE_AUDIT_20260813.md`, and
  `SECURITY_AUDIT_20260804.md` are historical audit inputs, not current-state
  guarantees.
- Paper-trial, paper-lane, autopsy, and promotion artifacts describe observed
  evidence; they do not reactivate killed strategies.

## Default rule

Any document not explicitly active above is `REFERENCE / RESEARCH_ONLY` unless
current code and a reviewed promotion record say otherwise. When documentation
and runtime policy disagree, fail closed and follow runtime policy.
