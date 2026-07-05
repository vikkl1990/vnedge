# Research Agents And Multi-Exchange Lanes

VNEDGE's research agents are bounded, local assistants for the slow loop. They
rank profitable exchange/symbol lanes, explain rejected lanes, and propose
whitelisted exploratory variants. They do not place orders, promote strategies,
alter paper-trial params, or bypass untouched-data judgment.

## Run

```bash
RESEARCH_EXCHANGES=binanceusdm,bybit,delta \
RESEARCH_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT \
.venv/bin/python -m vnedge.research.continuous_research
```

Per-exchange symbol overrides are supported:

```bash
RESEARCH_EXCHANGES=binanceusdm,bybit \
RESEARCH_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT \
RESEARCH_SYMBOLS_BYBIT=BTC/USDT:USDT,SOL/USDT:USDT \
.venv/bin/python -m vnedge.research.continuous_research
```

## Output

`research/live_research/latest.json` now includes:

- `universe`: exchange/symbol/timeframe coverage for the cycle.
- `results`: exchange-aware walk-forward records.
- `edge_agents.profitable_pairs`: best currently profitable lane per
  exchange/symbol.
- `edge_agents.proposals`: exploratory follow-ups, including pre-registered
  judgment prompts, cross-exchange validation prompts, and whitelisted variant
  backtests.
- `scalper_research`: tick/L2 replay diagnostics and recorder targets.
- `alpha_factory`: structural alpha hypotheses and replay queue. See
  `docs/ALPHA_FACTORY.md`.
- `edge_agents.policy`: the hard safety policy. `can_trade=false`,
  `can_promote=false`, and untouched-data judgment remains required.

## Scalper Scanners

Scalping uses a separate tick/L2 scanner because candles cannot prove
microstructure edge. The scanner is research-only and ranks lanes for recorder
and replay attention:

```bash
.venv/bin/python -m vnedge.research.scalper_scanners \
  --exchanges binanceusdm,bybit \
  --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT \
  --days YYYYMMDD
```

Full exchange-wide derivative discovery:

```bash
.venv/bin/python -m vnedge.research.scalper_scanners \
  --all-markets \
  --exchanges binanceusdm,bybit,delta \
  --quote-assets USDT,USDC,USD \
  --days YYYYMMDD \
  --limit 200
```

It emits:

- `recorder_priority`: which exchange/symbol lanes deserve more tick/L2 data.
- `edge_score`: how close replay evidence is to a research candidate.
- `route_decision`: `BLOCKED`, `MAKER_ONLY`, or `TAKER_ALLOWED`, based on PF
  and net bps after maker/taker/slippage costs.
- `state`: `MISSING_TICK_DATA`, `RECORD_MORE`, `REPLAY_CANDIDATE`, or the
  rejection class (`REJECTED_COST_WALL`, `REJECTED_NO_FILLS`, etc.).
- `recorder_targets`: unique exchange/symbol lanes to arm next.

Hard policy: `can_trade=false`, `can_promote=false`. A `REPLAY_CANDIDATE` still
requires pre-registered untouched replay before paper/shadow discussion.
Rows below PF/breakeven are `BLOCKED`; they are not weak signals.

## Scalper Edge Miner

Use the edge miner when scanners say "edge is missing." It searches recorded
tick/L2 data for microstructure hypotheses before strategy promotion:

```bash
.venv/bin/python -m vnedge.research.scalper_edge_miner \
  --all-markets \
  --exchanges binanceusdm,bybit,delta \
  --quote-assets USDT,USDC,USD \
  --days YYYYMMDD \
  --limit 100
```

It tests pressure continuation, absorption reversal, and microprice
continuation across forward horizons. Results are still research-only and use
the same route gate: below PF/breakeven means `BLOCKED`.

## Alpha Factory

Use the alpha factory to mine structural hypotheses from recorded tick/L2 tape:

```bash
.venv/bin/python -m vnedge.research.alpha_factory --days YYYYMMDD --limit 100
```

It looks for forced-flow continuation, absorption reversal, microprice
dislocation, liquidity-vacuum continuation, and volatility impulse families.
Any positive result is a replay queue item, not a signal. Conservative replay,
untouched judgment, and human paper/shadow approval remain mandatory.

## Guardrails

- A rolling PASS is only a candidate signal.
- Positive rejected lanes can be ranked as interesting, but remain rejected.
- Agent variants come from `strategy_diagnostics.CATALOG`; no arbitrary search.
- Auto-explore records are marked `auto=true` and cannot be promoted directly.
- The running paper trial is never retuned by this loop.
- Scalper scanners guide data collection and replay only; they never route
  signals into execution.
- The edge miner creates hypotheses only; no mined edge can trade without
  untouched replay, paper, shadow, and gateway approval.
- The alpha factory creates structural hypotheses only; no hypothesis can trade
  without conservative replay, untouched judgment, paper, shadow, and gateway
  approval.
