# Kronos Forecast Gate

## Why Kronos Is Useful

[Kronos](https://github.com/shiyu-coder/Kronos) is a foundation model for
financial candle sequences. It uses an OHLCV tokenizer plus an autoregressive
Transformer to forecast future K-line paths.

For VNEDGE, the useful primitive is not "model says buy" or "model says sell".
The useful primitive is:

> Given the last closed candle window, does the forecasted future path have
> enough expected room to clear exchange fees, slippage, and adverse path risk?

That makes Kronos a **forecast gate** above existing scanners, not a standalone
trading strategy.

## What This PR Adds

`vnedge.research.kronos_forecast_gate` scores already-generated forecast paths:

- auto-selects long/short unless a side is supplied
- supports maker-taker and taker-taker cost assumptions
- measures terminal move, favorable move, adverse move, reward/risk, confidence
- requires expected net edge after costs and safety buffer
- stays read-only: `can_trade=false`, `can_promote=false`

The module does **not** import Kronos, Torch, or Hugging Face. A separate runner
can generate forecasts with Kronos and pass CSV/JSON paths into this gate.

## Safe VNEDGE Integration

Recommended path:

1. Generate Kronos forecast paths offline for BTC/ETH/SOL/XRP/DOGE across
   5m, 15m, 1h, and 4h.
2. Feed those paths into `kronos_forecast_gate_v1`.
3. Join the gate result to existing scanner opportunities as an ex-ante feature.
4. Compare OOS:
   - raw scanner baseline
   - scanner + Kronos veto
   - scanner + Kronos route selector
5. Promote only if it improves fee-aware OOS results and survives the normal
   untouched-window review.

## Guardrails

- No model output can bypass `PreTradeRiskGateway`.
- No model output writes runtime manifests.
- No paper lane is created from this module alone.
- Taker is allowed only if forecast net edge clears taker cost plus buffer.
- Forecasts must be judged chronologically; no random split or hindsight replay.

## Why Not Add Kronos Directly To Runtime?

Kronos model inference requires Torch/Hugging Face model weights and can be
expensive on a small VM. Loading it into the live hot path would add operational
fragility before proof of edge. VNEDGE should first prove the value as a
research gate and only later decide whether forecasts run on the VM, a sidecar,
or an offline scheduled job.
