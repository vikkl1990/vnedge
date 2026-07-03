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
4. ~~Strategies: first candidates~~ ✅ built + judged: indicators (causal,
   NaN-warmup convention), regime classifier (ER + EMA alignment, ATR
   percentile), trend_continuation_v1 and funding_mean_reversion_v1, registry.
   365d walk-forward verdicts (2026-07-02): ALL FOUR symbol/strategy combos
   REJECTED by promotion gates. Notable: funding MR on BTC was OOS-positive
   (+$55.73) but failed zero-trade-window gates — sparse event strategies may
   need longer test windows, a change to PRE-REGISTER for the next research
   round, never applied retroactively to promote a seen result. Strategy
   iteration continues in milestone 8; do NOT relax gates to pass a candidate.
5. ~~Order manager core: state machine, persisted idempotency, decision
   journal~~ ✅ (TIMEOUT_UNKNOWN blocks new risk until reconciliation
   resolves it; journal-before-submit; journal unavailable ⇒ exits only;
   duplicate intents dropped). REMAINING for m5: cancel/replace, partial-fill
   accounting, adapter-level bounded retries reusing client_order_id,
   emergency flatten.
6. ~~Paper broker: simulated exchange + adapter + reconciliation + emergency
   flatten~~ ✅ (idempotent venue, pessimistic fills, timeout_reached vs
   timeout_lost both resolved via reconciliation; kill switch is now
   exits-only in the gateway so flatten flows through the normal pipeline).
7. ~~Paper/shadow runner loop~~ ✅ (one loop, both modes — a separate shadow
   runner would be a second execution path; portfolio tracker feeds real
   AccountState; daily-loss limit is now min(fixed USD, % of peak equity);
   OrderManager.submit accepts `now` for replay/exchange-synced clocks).
   KNOWN DIVERGENCE (by design, surfaced by compare_to_backtest on real
   BTC data): paper enforces the full risk layer (daily-loss halt,
   consecutive-loss breaker) that the backtester does not model — paper
   trades ≤ backtest trades. Future option: apply gateway policies inside
   the backtester so research matches operations.
8. ~~Live market feed + live paper session~~ ✅ (CCXT Pro websockets:
   closed-candle discipline, order-book-top quotes — venue ticker streams
   may lack bid/ask, funding via periodic REST; honest staleness = wall
   clock since last WS event, so the gateway's freshness check works for
   real; validated live against Binance: full pipeline order on streaming
   data). NOTE: LiveMarketFeed/LivePaperSession are v1 — exits are
   bar-close granular (tick-level stop monitoring is a live-adapter-phase
   upgrade), and prepare() re-runs per bar (fine at 1m+, optimize later).
   REMAINING before live trading: live ExecutionAdapter (real orders,
   testnet first), pre-live checklist, a strategy that passes gates.
7b. ~~Monitoring dashboard per DESIGN.md §6~~ ✅ (read-only FastAPI app:
   GET /state + 1-2Hz snapshot WS, bearer token mandatory, zero control
   routes — tested; vanilla single-page UI; demo replay via
   `python -m vnedge.dashboard.demo`, preview config in .claude/launch.json)
8. ~~Strategy research round 2~~ ✅ ran 2026-07-02, all six combos REJECTED:
   - Trend on 4h (±low-vol floor): mostly UNTESTABLE — <5 in-sample trades
     per train window, so selection can't even run. 4h trend at this horizon
     is too sparse for 90d/30d windows; not disproven, not supported.
   - Funding-MR 30d windows: ETH clearly negative twice in a row (round 1
     −$99, round 2 −$106, PF≈0.5) — ETH is DEAD for this strategy. BTC
     OOS-positive AGAIN (+$54.60, 22 trades, 6 windows; round 1 +$55.73) but
     rejected on a single zero-trade window — same gate both rounds.
   ROUND 3 PRE-REGISTERED (do not modify after seeing results):
   - Sparse-strategy gate variant: drop the per-window zero-trade rule;
     require instead ≥10 aggregate OOS trades AND ≥60% of windows with ≥1
     trade. All other gates unchanged.
   - Judged ONLY on untouched data: BTC, the 365d period ENDING 365d ago
     (needs downloader --until support). Current-year BTC data has now been
     seen twice and is burned for this decision.
   - If it passes there: eligible for paper trading, human approval next.
   ROUND 3 VERDICT (2026-07-03, one run, untouched BTC 2024-07-03→2025-07-03):
   funding_mean_reversion_v1 **PASSED** — under the pre-registered
   SPARSE_STRATEGY_GATES and also under standard gates (7/7 windows traded,
   31 OOS trades, +$16.00 net, 57% profitable windows, worst window −$12.08,
   worst DD 4.7%). Third consecutive OOS-positive BTC result across
   independent data slices (+$55.73, +$54.60, +$16.00). HONEST CAVEATS: thin
   edge, single symbol, fee-sensitive, selection favored extreme_pct=0.85 /
   z_entry=1.5 in 6/7 windows. STATUS: eligible for live-data paper trading;
   HUMAN APPROVAL REQUIRED before starting; live capital remains gated by
   the full pre-live checklist regardless.
