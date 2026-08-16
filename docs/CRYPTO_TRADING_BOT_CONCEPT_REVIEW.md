# `crypto-trading-bot` concept review

Reference reviewed: [vikkl1990/crypto-trading-bot](https://github.com/vikkl1990/crypto-trading-bot) at commit
[`a2a6cb64e99afe06a23f92871385f8c678bf08b1`](https://github.com/vikkl1990/crypto-trading-bot/commit/a2a6cb64e99afe06a23f92871385f8c678bf08b1).

The reference repository did not expose a `LICENSE` or `COPYING` file at the reviewed commit.
VNEDGE therefore copies no source code. The additions below are independent implementations of
general market-measurement and risk-control concepts.

## Accepted concepts

| Concept | VNEDGE implementation | Safety boundary |
|---|---|---|
| Closed-bar regime context | `vnedge.data.regime_context` measures ADX, ATR percentile, EMA slope, Bollinger width, and volume ratio on canonical 1h/4h candles | Measurement only; malformed, forming, gapped, or degraded series fail closed |
| Regime-aware S1 diagnostics | `StructureContext` accepts optional immutable 1h/4h regime measurements; S1 records the vector without producing a composite score | Regime can block an intent, never grant one |
| Fee-wall room check | `CostGate` can require available target room to exceed a frozen multiple of estimated round-trip cost | Runs before sizing; missing room fails closed when enabled |
| ATR displacement hygiene | Existing S1 true-range ATR and frozen ATR stop cap retained | No runtime optimization or adaptive leverage |
| Session tags | Existing Market Pulse/session-regime implementation retained | Descriptive UI/research context |
| Paper cost parity | Existing VNEDGE fee, slippage, funding, and India GST models retained | No zero-slippage or free maker-fill assumptions |

## Explicitly rejected

- scanner or indicator farms;
- liquidity/OB/CVD entry replication;
- online strategy weighting or self-promotion;
- strategy-controlled sizing, leverage, or order placement;
- “allow while insufficient data” behavior;
- exact maker fills or zero-slippage paper assumptions;
- latency-arbitrage logic inside the directional VNEDGE runtime.

## Resulting flow

```text
trades -> canonical closed candles -> regime + structure + AVWAP measurements
       -> rare S1 research intent -> CostGate net-edge AND room checks
       -> registry / promotion controls -> journal
```

No component added by this review can set `tradeable=True`, mark capital eligibility, or emit an
`OrderIntent`.
