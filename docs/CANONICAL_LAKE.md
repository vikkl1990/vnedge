# VNEDGE canonical lake contract

VNEDGE has one market truth: public Delta trades and BBO. Trades build the
immutable candle ladder; BBO is a separate acceptance/protection tape. Neither
funding nor heartbeat frames may enter OHLC, volume, or VWAP.

## One tape, one ladder, many clocks

```text
Delta trades ──> durable trade shards ──> closed 1m
                                             │
                                             └─> 5m ─> 15m ─> 1h ─> 4h ─> 1d ─> 1w
Delta BBO    ──> durable quote evidence ──────────────> quote hold / chase / stops
```

The Delta owner subscribes to trades and `ob_l1` on one public socket. Raw L1
rows are persisted beside the trade tape with exchange/receipt clocks,
sequence when supplied, sizes in contracts, and `overflow_drops`. They are
tagged `evidence_scope=recorder_raw` and `parity_eligible=false`: only the
separately captured lane-consumed sequence can prove acceptance parity.

Every rollup requires the complete consecutive child set. Empty trade buckets
remain absent. A missing child therefore produces no parent. Weekly identity is
seven complete UTC days beginning Monday 00:00.

Delta trade `size` is contracts. The raw shard retains `size_contracts`,
`contract_value`, and `base_amount`; only `base_amount = size_contracts ×
contract_value` enters candle volume. BTCUSD uses 0.001 BTC per contract and
ETHUSD uses 0.01 ETH per contract from the reviewed product baseline.

## Immutable bar identity

Physical partitions retain the existing Decimal OHLCV columns and schema-v2
metadata:

```text
source             canonical_tick_lake | official_delta_ohlc | repaired
content_sha256     hash of normalized evaluated OHLCV + source
data_quality       ok | gap | partial
coverage_ok        boolean
parent_open        immediate parent bucket identity
is_closed          true for every persisted row
```

`CandleParquetStore.get_bar(symbol, timeframe, open_time)` is exact. It never
returns an earlier bar. Legacy numeric partitions disclose
`identity_persisted=false` until explicitly migrated.
When a new Delta bar first touches a legacy partition, old rows are migrated as
`data_quality=partial` and `coverage_ok=false`; that schema rewrite is not an
attestation of their historical contract units.

Audit without mutation:

```bash
python -m vnedge.data.lake_contract audit \
  --exchange delta_india --symbols BTCUSD,ETHUSD --timeframes 15m,4h,1d
```

Stamp known canonical legacy partitions without changing OHLCV:

```bash
python -m vnedge.data.lake_contract stamp \
  --exchange delta_india --symbols BTCUSD,ETHUSD --timeframes 1m,5m,15m,1h,4h,1d \
  --source canonical_tick_lake
```

Never use `stamp` to relabel official OHLC as canonical. Repair writes a new
explicit source/hash; it is not a silent edit.

Historical Delta bars must be rebuilt from the authoritative raw-tape window
before they are trusted, because older recorders persisted contract counts as
base volume. Stop the sole writer, then run under the canonical writer lease:

```bash
python -m vnedge.data.candle_bootstrap \
  --source-exchange delta_india --target-exchange delta_india \
  --symbols BTC/USD:USD,ETH/USD:USD --days 7 --replace-existing
```

Only complete parents emitted by that replay regain `coverage_ok=true` and a
new content hash. Do not run `stamp` as a substitute for this repair.

## Backfill boundary

Official Delta 4h/1d OHLC may seed denial-only HTF context when the registered
strategy contract explicitly allows `exchange_ohlcv_validated`. It may not
replace live 15m decision bars and cannot prove trade VWAP. Official context is
hash-bound with its own source tag before the regime machine sees it.

Daily EMA200 needs at least 200 closed daily observations; 730 is the preferred
operational history. A forming daily never updates committed EMA state.

## Readiness

`/ready` is service-workflow readiness, never permission to trade. When Delta
shadow lanes exist it aggregates those lanes—not a primary Binance measurement
feed—and exposes data, decision, parity, execution, and live layers separately.
It also publishes per-lane identity, daily-bar count, EMA200 readiness, and
missing context timeframes. Capital stays locked until the independent live
checklist is complete.

Router authority remains dark until router and Parquet match non-idle bar hashes
and decision identities over the required observation window.
