# VNEDGE — working conventions

Crypto F&O/perpetuals trading assistant. Safety-first: capital protection beats
profit, always. Nothing here is financial advice.

## Locked decisions (2026-07-02)

- Exchanges: Binance Futures (dev/validation, testnet first), Delta Exchange
  India (first live candidate), Bybit (third). Jurisdiction: India.
- Hybrid framework: Freqtrade/FreqAI for strategy research; custom
  CCXT/asyncio stack (this repo) for execution.
- Capital design point: < $1,000. Daily loss halt: fixed USD, default $20.
- Leverage: default 5x, >10x needs `acknowledge_high_leverage=true`, hard
  ceiling 30x (`ABSOLUTE_MAX_LEVERAGE` in risk_config.py — changing it is a
  reviewed code change, not a config change).
- Position size comes from risk-per-trade and stop distance, never leverage.
- Deployment target: Linux VPS + Docker. Dev on macOS.

## Invariants — do not break these

- Every order passes `PreTradeRiskGateway.evaluate()`. No bypass path, ever —
  including any future IPC/service layer; margin-only side checks don't count.
- Live orders require THREE gates: a live_* mode, `live_trading_enabled=true`,
  and `confirm_live_trading=I_UNDERSTAND_THIS_IS_HIGH_RISK`.
- Mode ladder: backtest → paper → shadow → live_small → live_full; each step
  validated before the next. `emergency_reduce_only` allows exits only.
- Idempotency keys are minted once per order intent, persisted to the decision
  journal, and reused verbatim on retry. Never re-derived from timestamps.
- Reconciliation mismatch ⇒ fail closed: stop new entries, go reduce-only,
  rebuild state from the exchange, resume only after a clean pass.
- Reduce-only exits must never be blocked by entry-quality checks.
- Kill switch never auto-resets; `touch KILL` in cwd trips it.
- API keys are trade-only; secrets only via env/.env (gitignored).
- Sizing rounds DOWN to exchange steps; too-small results are rejected, never
  inflated to meet minimums.
- Risk configs are frozen; limit changes require restart.

## Code conventions

- Python 3.11+, type hints everywhere, pydantic v2 for config/validation.
- Frozen dataclasses for state snapshots (OrderIntent, AccountState, ...).
- Decisions must be explainable: rejections carry every failed check, not
  just the first.
- No silent failures, no hardcoded secrets, no default-enabled live paths.
- Run `.venv/bin/python -m pytest -q` before considering any change done.
- Risk-critical code gets tests in the same change, not later.

## Architecture decisions (2026-07-02 review)

V1 is a SINGLE-PROCESS asyncio application. The portfolio/risk state is
therefore naturally single-writer — no IPC needed. Explicitly rejected for v1
(revisit only with evidence of need): UDS risk daemons, NATS/Redpanda event
bus, per-exchange processes, CPU pinning, ONNX C-API hot paths, sub-3ms
latency targets (network RTT to the exchange is 10–100ms; our strategies live
at seconds-to-hours timescales), options trading (v3 at earliest).

Adopted from the same review: operating-mode ladder incl. shadow mode,
three-gate live confirmation, market data quality gate (sequence/checksum/
staleness/clock-skew), order state machine with persisted idempotency keys,
append-only JSONL decision journal (WAL), fail-closed reconciliation,
human-gated strategy promotion (no auto hot-swap).

V1 live scope: ONE exchange, BTC + ETH USDT perps only. Multi-exchange and
the wider universe come after v1 is proven.

Tax/compliance: record complete immutable fill/fee/funding data; do NOT
hardcode Section 194S/TDS logic — perp fills are not obviously VDA transfers;
needs CA sign-off first.

Implementation contracts for milestones 2–6 (data quality gate checks, order
state machine incl. TIMEOUT_UNKNOWN handling, reconciliation scope, WAL
rules) live in docs/DESIGN.md — follow them when building those modules.

## Build order (next milestones)

1. ~~Foundation: config + risk core + mode gates~~ ✅
2. ~~Data layer: CCXT candle/funding/OI ingestion → Parquet store, with the
   data quality gate at the boundary~~ ✅ (validated live vs binanceusdm;
   `python -m vnedge.data.download --days 90`; note Binance OI history is
   clamped to ~29d lookback)
3. ~~Backtester: fee/slippage/funding-aware core + walk-forward~~ ✅
   (decisions at close, fills at next open — lookahead structurally
   impossible; sizing reuses risk/position_sizer.size_position so backtest
   and live can't diverge; stop wins stop-vs-TP ties; walk_forward.py does
   rolling train/test with OOS-only judgment, min-trade-count selection,
   no equity compounding across windows).
4. Strategies: regime-filtered hybrids (research in Freqtrade)
5. Order manager: state machine, persisted idempotency, decision journal
6. Paper broker → shadow mode → reconciliation engine → live_small
   (gated by the 10-point pre-live checklist in README)
