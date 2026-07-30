# Quantified Strategy Lab

VNEDGE now tracks the operator-supplied QuantifiedStrategies 95-strategy title
inventory as a finite research queue.

This is deliberately **title-only research**. The pack list is useful because it
names proven research themes, but it does not give VNEDGE executable rules. The
lab does not copy paid or proprietary rules, does not emit trading signals, and
does not promote lanes. Each title becomes one of three things:

- a VNEDGE-owned crypto hypothesis,
- a crypto session/relative-strength study,
- or a quarantined asset-specific idea.

## How To Publish

```bash
python -m vnedge.research.quantified_strategy_lab
```

Default artifacts:

- `research/live_research/quantified_strategy_lab_latest.json`
- `research/live_research/quantified_strategy_lab_feed.jsonl`

Dashboard:

- `/quantified-strategy-lab`
- `/quantified-strategy-lab/kb`
- `/quantified-strategy-lab/port-factory`

Both dashboard surfaces are read-only and dashboard-token gated.

## Port Factory

The port factory turns the title lab into durable Quant OS research tasks:

```bash
python -m vnedge.research.quantified_port_factory
```

Default artifacts:

- `research/live_research/quantified_port_factory_latest.json`
- `research/live_research/quantified_port_factory_feed.jsonl`

By default it queues Chunks A-D into the Quant OS agent gateway. Chunk Q remains
quarantine-only. Re-running the publisher reuses existing tasks by stable
blueprint id and writes a new content artifact only when the blueprint hash
changes.

## Port Families

The lab groups the 95 titles into VNEDGE-owned build families:

- `bitcoin_crypto_strategy_pack_v1`
- `range_volatility_breakout_reversion_v1`
- `pullback_reversion_pack_v1`
- `indicator_pack_mtf_v1`
- `trend_momentum_pack_v1`
- `price_action_structure_pack_v1`
- `crypto_session_calendar_miner_v1`
- `crypto_relative_strength_rotation_v1`
- `short_tail_risk_pack_v1`
- `ensemble_blend_lab_v1`
- `swing_template_crypto_rebuild_v1`

## Fast Track

Chunk A should be built first: crypto-native, breakout/reversion, and pullback
families. Those map best to the bot's current scanner stack and to the active
TP1/breakeven/trailing exit work.

Chunk B converts classic indicators into edge-model features rather than hard
entry gates.

Chunk C remaps equity overnight/month-end/weekday ideas to crypto-native clocks:
UTC day boundary, Asia/London/NY sessions, funding windows, and weekend
liquidity changes.

Chunk D rebuilds rotation ideas as cross-pair crypto relative strength.

Chunk Q is quarantine: bond, DAX, ETF-sector, placeholder, and unreleased titles
stay research-only until a clean crypto thesis exists.

## Promotion Contract

A port is not tradable from this lab until it has:

- causal VNEDGE implementation,
- 1m/5m/15m/1h/4h replay where data exists,
- maker/taker/slippage fee-wall accounting,
- active TP1/TP2/TP3 and breakeven/trailing comparison,
- expected net edge greater than 25 bps,
- PF greater than 1.5,
- at least 20 historical trades,
- untouched-window judgment.

This lab is a factory intake, not an execution permission surface.
