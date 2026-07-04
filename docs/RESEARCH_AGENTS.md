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
- `edge_agents.policy`: the hard safety policy. `can_trade=false`,
  `can_promote=false`, and untouched-data judgment remains required.

## Guardrails

- A rolling PASS is only a candidate signal.
- Positive rejected lanes can be ranked as interesting, but remain rejected.
- Agent variants come from `strategy_diagnostics.CATALOG`; no arbitrary search.
- Auto-explore records are marked `auto=true` and cannot be promoted directly.
- The running paper trial is never retuned by this loop.
