# Quant Signal Pack

`quant_signal_pack_v1` is the VNEDGE implementation of the commercial-style
signal stack operators asked for: Lux/Willy-like concepts translated into
causal, testable bot logic. It does not copy proprietary TradingView/Pine
scripts. It converts the visual ideas into auditable research features.

## Included Families

- SMC structure: break of structure and change of character proxies.
- Liquidity: sweep/reclaim of prior liquidity pools.
- Fair value gap: bullish/bearish FVG creation and later retest.
- Order block proxy: opposite candle followed by displacement through its range.
- Squeeze momentum: volatility compression followed by volume-backed release.
- VWAP reversion: stretched move reclaiming rolling VWAP.
- Multi-horizon bias: fast/mid/slow EMA alignment plus efficiency ratio.
- Volume impulse and displacement filters.

## Safety Line

The pack emits normal `SignalIntent` objects only after closed-candle
evaluation. Every signal has:

- side
- stop
- target
- reason string listing active features

It does not bypass the risk gateway, journal, order manager, or promotion
machinery. A rolling PASS is still only a candidate. Untouched-data judgment and
human approval remain required before paper/live promotion.

## Research Runtime

The continuous research loop now sweeps `quant_signal_pack_v1` under
`OFFENSIVE_GATES` across the configured universe. Runtime shadow lanes can be
created only through the research-to-shadow manifest, and those lanes remain
`can_trade=false` / `can_promote=false`.

Primary module:

```bash
src/vnedge/strategy/quant_signal_pack.py
```

Representative tests:

```bash
tests/test_quant_signal_pack.py
```
