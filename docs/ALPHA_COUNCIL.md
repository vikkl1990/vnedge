# Alpha Council

The Alpha Council is VNEDGE's agent-debate layer for strategy discovery. It is
inspired by agent-native trading platforms where many agents publish and
challenge ideas, but it is deliberately research-only.

## What It Does

Every cycle, `python -m vnedge.research.alpha_council` reads the existing
research artifacts in `research/live_research/`:

- `latest.json` rolling walk-forward research
- `event_leadlag_latest.json`
- `l2_scout_latest.json`
- `l2_latest.json`
- `daily_scalper_latest.json`
- `alpha_distillation_latest.json`

It extracts candidates, then runs five deterministic agents:

- `edge_advocate`: argues for the edge evidence
- `skeptic`: attacks sample size, overfit, weak gates, and unresolved rejects
- `execution_specialist`: checks maker/taker feasibility and fee wall
- `risk_governor`: applies promotion-ladder vetoes
- `research_director`: converts the debate into the next proof step

The output is written to:

- `research/live_research/alpha_council_latest.json`
- `research/live_research/alpha_council_feed.jsonl`

The Alpha Workbench consumes this output and checkpoints each next-action into
durable replay, judgment, or recording tasks under
`research/live_research/alpha_workbench/`. The council debates; the workbench
remembers the work.

## What It Never Does

- No orders
- No paper/shadow/live promotion
- No parameter mutation
- No model hot swap
- No bypass of `PreTradeRiskGateway`

The council may rank an idea as high-priority, but the payload always has
`can_trade=false` and `can_promote=false`.

## Why This Shape

For scalping, the failure mode is not a lack of ideas; it is false confidence.
The council therefore separates roles:

- the advocate keeps us from ignoring weak-but-real pulses
- the skeptic keeps us from overfitting
- execution keeps the fee wall and fill assumptions explicit
- risk keeps the promotion ladder intact

The next phase is to let sandboxed AI proposal agents generate candidate
families, but those proposals should feed this council as evidence rows and
still require replay, untouched judgment, shadow, and paper validation.
