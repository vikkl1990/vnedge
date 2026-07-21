# VNEDGE Algo ML Pro Scanner

`vnedge_algo_ml_pro_v1` is the VNEDGE-native scanner for the supplied
open/user-provided Pine source: `VNEDGE ALGO SuperTrend ML Pro + BBP`
(`v6.0.1`).

## What Was Ported

- Adaptive SuperTrend-style flip engine.
- Bull Bear Power: `high - EMA13`, `low - EMA13`, and normalized pressure.
- Auto HTF alignment: 5m uses 30m by default; 15m uses 1h.
- RSI momentum, ADX strength, volume impulse, volume-zone proximity, and
  divergence features.
- Pine-style ML confidence score from the 13 feature weights.
- Band/ATR/fixed stop geometry, TP1/TP2/TP3 ladder, and trailing-plan metadata.

## What Was Not Ported

- Chart-only dashboard tables, labels, background fills, line drawing, and
  spectral forecast visuals.
- Pine self-learning gate mutation in live time. VNEDGE keeps learning in the
  research layer so the live path remains auditable and reproducible.
- Any live permission. The scanner emits research/paper intents only.

## Paper Sizing Lens

The execution-edge router now reports paper USD PnL using:

- `paper_margin_usd = 100`
- `paper_leverage = 25`
- `paper_notional_usd = 2500`

This is a reporting lens only. It does not change live leverage caps, position
sizing, risk-per-trade, or the `PreTradeRiskGateway`.

Example one-lane replay:

```bash
python -m vnedge.research.execution_edge_router \
  --data-root data \
  --exchange delta_india \
  --symbol ETHUSD \
  --timeframe 5m \
  --strategies vnedge_algo_ml_pro_v1 \
  --lookback-days 30 \
  --horizon-bars 15 \
  --min-samples 20 \
  --min-edge-bps 25 \
  --min-profit-factor 1.5 \
  --paper-margin-usd 100 \
  --paper-leverage 25
```

Batch forensics:

```bash
python -m vnedge.research.fee_wall_forensics \
  --data-root data \
  --exchanges delta_india \
  --symbols ETHUSD \
  --timeframes 5m \
  --strategies vnedge_algo_ml_pro_v1 \
  --lookback-days 30 \
  --min-edge-bps 25 \
  --min-profit-factor 1.5 \
  --paper-margin-usd 100 \
  --paper-leverage 25 \
  --include-opportunities
```

## Promotion Rule

This scanner remains research-only until an untouched-window judgment clears
the usual VNEDGE gates. For the requested scalper profile the promotion target
is:

- expected net edge greater than `25 bps`;
- profit factor greater than `1.5`;
- at least `20` historical routed trades;
- taker only when forecast edge covers fees, slippage, and safety buffer.

## First VM Backtest

Run on the VM against normalized Delta India `ETHUSD` candles with the paper
reporting lens `100 USD margin x 25x = 2,500 USD notional`.

Command scope:

- exchange: `delta_india`
- symbol: `ETHUSD`
- timeframes: `1m, 5m, 15m, 1h, 4h`
- lookback: `30d`
- strategy: `vnedge_algo_ml_pro_v1`
- gate: `25 bps`, `PF > 1.5`, `min 20 routed trades`

Results:

| Timeframe | Routed | Avg Net | PF | Fee-Wall Break | Verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| 1h | 13 | +22.98 bps | 1.60 | 100.00% | sparse positive, below 20 trades and below 25 bps |
| 15m | 89 | +6.51 bps | 1.25 | 89.89% | positive but below edge/PF gate |
| 5m | 88 | -8.12 bps | 0.68 | 76.14% | negative after cost |
| 1m | 229 | -16.10 bps | 0.45 | 72.49% | negative after cost |
| 4h | 0 | -- | -- | 0.00% | no opportunities |

Interpretation: the scanner fires, but the raw 5m/1m form gives back too much
after fees. The useful evidence is not "promote as-is"; it is that many entries
show positive MFE after costs, so the next research uplift should test faster
target capture, BE-after-TP1, and trail tightening before trying to promote the
lane.
