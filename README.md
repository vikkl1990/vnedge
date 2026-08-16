# VNEDGE — Crypto F&O / Perpetuals Trading Assistant

An explainable, risk-controlled crypto derivatives trading assistant. Not a grid
bot, not a leverage casino, not a profit machine.

> **This is not financial advice. Crypto futures and perpetuals are high-risk
> instruments. Every strategy in this system must be backtested, paper traded,
> and deployed with small capital first. Total loss of trading capital is a
> realistic outcome.**

## Project charter (decided 2026-07-02)

| Decision            | Value                                                                 |
|---------------------|-----------------------------------------------------------------------|
| Exchanges           | Binance Futures, Bybit, Delta Exchange India (multi-exchange design)  |
| Jurisdiction        | India (FIU-registered venues; consult a CA for VDA tax / TDS treatment) |
| Instruments         | USDT-margined perpetuals; start BTC/ETH, expand via liquidity filters |
| Framework           | Hybrid — Freqtrade/FreqAI for research, custom CCXT/asyncio for execution |
| Capital design point| Micro: under $1,000                                                   |
| Daily loss halt     | Fixed USD amount (default **$20/day**, configurable)                  |
| Leverage            | Default 5x, >10x requires explicit acknowledgment, absolute cap 30x   |
| Position sizing     | Risk-based (% of equity to stop), never leverage-based                |
| Deployment          | Linux VPS + Docker (dev on macOS)                                     |
| Default runtime     | Public-data measurement only; no signal-producing strategy            |
| Paper capital       | Empty roster; explicit approval set currently empty                     |
| Live trading        | **Not deployed.** Manual guarded entrypoint only                       |

## Current product boundary

- `docker compose up` runs no-signal measurement lanes and the authenticated
  dashboard. It has no live adapter.
- `docker compose --profile research up research-loop` adds quality-gated
  ingest and walk-forward evidence. Research cannot mutate the runtime roster.
- Paper capital requires both `MULTI_LANE_CAPITAL_ENABLED=1` and an explicit
  `MULTI_LANE_CAPITAL_STRATEGY`, and that ID must also appear in the reviewed
  `CAPITAL_APPROVED` set. The set is currently empty. Registration or absence
  from `KILLED` is not capital permission.
- The manual live entrypoint remains fail-closed and currently refuses every
  strategy because `CAPITAL_APPROVED` is empty. Independently, Delta cannot
  start live until a native private order/fill stream exists.

See [the architecture flow](docs/ARCHITECTURE_FLOW.md).
Documentation status and precedence are defined in
[docs/STATUS_INDEX.md](docs/STATUS_INDEX.md); unlisted strategy notes are
research references, never runtime permission.

## Exchange sequencing

- **Binance Futures** first: best documentation and deepest production
  liquidity — the development and validation venue for real market-data
  paper/shadow lanes. Testnet/sandbox liquidity is not used for edge proof.
- **Delta Exchange India** second: India-domiciled, smaller contract sizes that
  suit micro capital, candidate first live venue.
- **Bybit** third, once the `BaseExchange` interface is proven by two implementations.

## Micro-capital reality check

With < $1,000: Binance BTCUSDT minimum order is 0.001 BTC (≈ $100+ notional), so
risk-per-trade math must check minimum notional *before* signal generation, and
some symbols will simply be untradeable at this size. Delta India's smaller
contracts are friendlier here. Fees and funding dominate at this scale — every
strategy is evaluated on after-cost expected value.

## Layout

```
src/vnedge/
  config/    settings, exchange config, risk config (pydantic, env-driven)
  risk/      kill switch, pre-trade gateway, position sizer
  exchange/  read-only account + public/private venue adapters
  data/      candle/funding/OI stores
  strategy/  regime filter + hybrid strategies
  backtest/  fee/slippage/funding-aware engine
  paper/     simulated broker
  execution/ order manager, WAL, reconciliation, fill ledger
  runtime/   measurement, paper/shadow, guarded live entrypoints
  dashboard/ authenticated read-only operator UI
tests/
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then edit — never commit .env
pytest
```

Tests marked `network` construct real exchange clients (ccxt / ccxt.pro /
native websockets). They run in the default suite; for a fully offline
environment deselect them with `pytest -m "not network"`.

## Non-negotiable safety rules

- API keys are trade-only. Withdrawal permission is never enabled. IP whitelist on.
- Secrets live in `.env` / a vault, never in code or git.
- Every order passes the pre-trade risk gateway. No bypass path exists.
- Kill switch (programmatic + `KILL` file) flattens and halts everything.
- No martingale, no averaging down without invalidation, no stop-less strategies.
- Live mode requires: backtest ✓, out-of-sample ✓, walk-forward ✓, paper ✓,
  risk config review ✓, kill-switch test ✓, reconciliation test ✓, small-capital
  approval ✓.
