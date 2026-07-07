# Alpha Distillation Pack

`alpha_distillation_pack_v1` is the research lane that turns public
indicator-stack ideas into VNEDGE-native alpha atoms. It does not copy
WillyAlgo, LuxAlgo, or TradingView/Pine logic. It translates the ideas traders
use manually into causal features that can be measured after fees.

## What It Distills

The inventory contains 35 public concepts grouped into nine atoms:

- `liquidity_sweep`
- `fvg_retest`
- `order_block`
- `squeeze_release`
- `vwap_reclaim`
- `structure_break`
- `trend_trail`
- `profile_reclaim`
- `momentum_impulse`

The important shift is that these are not standalone trade buttons. Each atom
is evidence in a scored setup:

```text
4h / 1h context
    -> 15m setup atoms
    -> 1m trigger confirmation
    -> expected edge bps versus maker/taker cost
    -> smart stop/target quality
    -> walk-forward verdict
```

## Fee-Wall Rule

The strategy will not emit a backtest intent unless the setup's estimated edge
clears the configured maker-first floor. Taker eligibility is explicitly
marked in the reason string, but a taker route still needs walk-forward,
untouched judgment, and human approval before any paper/shadow promotion.

The backtester remains conservative: entries and exits still use the existing
taker/slippage cost model. The route label is research telemetry, not an order
instruction.

## Smart Exit

Each signal uses a wick/structure anchored stop plus an ATR guard. The target R
is increased only when the setup score is stronger, bounded by
`max_take_profit_r`. Stops that are too tiny or too wide are blocked before the
intent exists.

## Run It

```bash
python -m vnedge.research.alpha_distillation \
  --data-root data \
  --out research/live_research/alpha_distillation_latest.json
```

Single lane:

```bash
python -m vnedge.research.alpha_distillation \
  --candidate 'binanceusdm|SOL/USDT:USDT|liquidity_sweep,fvg_retest|long'
```

Diagnostic only, if 1m trigger coverage is incomplete:

```bash
python -m vnedge.research.alpha_distillation --no-1m-trigger
```

## Governance

The report is always research-only:

- `can_trade=false`
- `can_promote=false`
- `requires_untouched_judgment=true`
- `requires_human_approval=true`

A PASS means "pre-register an untouched judgment window." It is not paper,
shadow, or live approval.
