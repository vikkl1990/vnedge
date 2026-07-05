# VNEDGE Architecture Flow

Status date: 2026-07-04.

This document maps the current VNEDGE system to the target scalper/research
vision. The core rule is unchanged: every execution order must pass
`PreTradeRiskGateway.evaluate()`, live trading remains behind the three live
gates, and AI/research agents never trade or promote strategies directly.

## Status Legend

| Status | Meaning |
|---|---|
| Current | Implemented on the main V1 path. |
| Branch | Implemented in an open feature branch/PR. |
| Next | Required next architecture work. |
| Deferred | Deliberately post-V1 until there is evidence of need. |

## Top-Level Flow

```mermaid
flowchart TB
    Operator["Human operator"] --> Dashboard["Read-only dashboard"]
    Dashboard --> StateSnapshot["Coalesced state snapshot"]

    subgraph Fast["I. Fast Execution Loop"]
        PublicFeed["Native public market feed"] --> QualityGate["Market data quality gate"]
        QualityGate --> MarketState["Market state cache"]
        MarketState --> Strategy["Strategy or model wrapper"]
        Strategy --> Risk["PreTradeRiskGateway.evaluate()"]
        Risk --> OrderManager["Order manager state machine"]
        OrderManager --> Journal["Decision journal / WAL"]
        OrderManager --> Adapter["Execution adapter"]
        Adapter --> Exchange["Exchange endpoints"]
        Exchange --> PrivateTruth["Private orders/fills/positions stream"]
        PrivateTruth --> Reconciler["Fail-closed reconciler"]
        Reconciler --> Portfolio["Portfolio and risk state cache"]
        Portfolio --> Risk
        Portfolio --> StateSnapshot
    end

    subgraph Medium["II. Medium Telemetry Loop"]
        PrivateTruth --> Normalizer["Orders / fills / fees / funding normalizer"]
        Normalizer --> Historian["Parquet historian"]
        Historian --> Drift["Live-vs-research drift monitor"]
        Drift --> Alerts["Alerts engine"]
        Alerts --> Dashboard
    end

    subgraph Slow["III. Slow Research And Agent Loop"]
        Universe["Research universe: exchanges x symbols"] --> Ingest["Public REST ingestion"]
        Ingest --> ResearchStore["Parquet research store"]
        ResearchStore --> WalkForward["Walk-forward gates"]
        WalkForward --> Diagnostics["Failure diagnosis"]
        Diagnostics --> EdgeAgents["Bounded edge research agents"]
        EdgeAgents --> AutoExplore["Whitelisted exploratory variants"]
        EdgeAgents --> CandidateList["Profitable pair candidates"]
        CandidateList --> Judgment["Human-approved untouched-data judgment"]
        Judgment --> PaperTrial["Paper / shadow trial"]
        PaperTrial --> Promotion["Mode ladder promotion"]
    end

    subgraph Audit["IV. Audit And Explanation Loop"]
        Journal --> AuditLedger["Immutable decision record"]
        Risk --> RejectReasons["Explainable reject reasons"]
        WalkForward --> ResearchFeed["Research feed JSONL"]
        EdgeAgents --> AgentPolicy["Agent policy: no trade, no promote"]
    end
```

## Current Execution Path

```mermaid
sequenceDiagram
    participant Feed as Public feed / candles
    participant Strategy as Strategy
    participant Gateway as PreTradeRiskGateway
    participant OM as OrderManager
    participant Journal as Decision journal
    participant Adapter as Paper or live adapter
    participant Recon as Reconciler

    Feed->>Strategy: closed candle or market snapshot
    Strategy->>Gateway: proposed order intent
    Gateway-->>Strategy: approve or reject with all failed checks
    Gateway->>OM: approved intent only
    OM->>Journal: persist intent and idempotency key before submit
    OM->>Adapter: submit with persisted client_order_id
    Adapter-->>OM: ack, reject, fill, or timeout_unknown
    OM->>Recon: unresolved orders require reconciliation
    Recon-->>OM: exchange truth resolves state
```

Execution invariants:

- No order path bypasses the gateway.
- Journal failure means no new risk-increasing entries.
- `TIMEOUT_UNKNOWN` blocks new risk until reconciliation resolves it.
- Reconciliation mismatch fails closed: entries stop, reduce-only remains.
- Kill switch never auto-resets.

## Scalper Target Flow

This is the practical scalper path that matches the vision without pretending
sub-3ms colocated execution exists in V1.

```mermaid
flowchart LR
    Trades["Streaming trades"] --> Micro["Microstructure state"]
    L2["L2 book builder"] --> Micro
    AllMarkets["All active linear perp/future markets"] --> Scanner["Scalper scanners\n(liquidity / flow / PF / route cost)"]
    Scanner --> Recorder["Tick/L2 recorder priorities"]
    Recorder --> Miner["Edge miner\n(pressure / absorption / microprice)"]
    Miner --> Scanner
    Recorder --> L2
    Private["Private fill/order stream"] --> Truth["Exchange truth cache"]
    Micro --> Features["Incremental feature engine"]
    Features --> Scalper["Scalper strategy interface"]
    Scalper --> ScalpRisk["Scalper risk overlay"]
    ScalpRisk --> Gateway["PreTradeRiskGateway.evaluate()"]
    Gateway --> Router["Order manager / hot router"]
    Router --> Exchange["Exchange"]
    Exchange --> Private
    Truth --> Reconcile["Fail-closed reconciliation"]
    Reconcile --> Gateway
    Truth --> Stops["Tick-level reduce-only stop engine"]
    Stops --> Gateway
```

Scalper-specific checks:

- Book/trade freshness.
- Spread and depth sanity.
- Edge after fees and slippage.
- Order-rate and cancel-rate limits.
- Private stream freshness.
- Reduce-only exits not blocked by entry-quality checks.
- Scanner approval is research-only; it never bypasses replay, paper, or the
  gateway.
- Maker/taker route is blocked unless replay PF and avg net bps clear the
  breakeven floor.

## Research Agent Flow

```mermaid
flowchart TB
    Targets["Research targets from env"] --> Refresh["Quality-gated refresh"]
    MarketDiscovery["CCXT all-market discovery"] --> ScalpScan
    Refresh --> Store["Parquet store"]
    Store --> Lanes["Strategy lanes"]
    Lanes --> Gates["Promotion gates"]
    Gates --> Records["Exchange-aware research records"]
    Records --> Profitable["Profitable-pair ranking"]
    TickData["Recorded tick/L2 days"] --> ScalpScan["Scalper scanner ranking"]
    ScalpScan --> RecorderTargets["Recorder targets"]
    TickData --> EdgeMiner["Microstructure edge miner"]
    EdgeMiner --> ReplayCandidates
    ScalpScan --> ReplayCandidates["Replay candidates"]
    Records --> Diagnosis["Reject diagnosis"]
    Diagnosis --> Agent["Bounded edge research agent"]
    Profitable --> Agent
    Agent --> Proposals["Exploratory proposals"]
    Proposals --> Variants["Whitelisted auto-variant backtests"]
    Proposals --> CrossVenue["Cross-exchange validation prompts"]
    Proposals --> JudgmentPrompt["Pre-registered judgment prompt"]
    ReplayCandidates --> JudgmentPrompt
    Variants --> Records
    JudgmentPrompt --> Human["Human approval"]
    Human --> Untouched["Untouched-data judgment"]
    Untouched --> Paper["Paper trial"]
```

Research-agent guardrails:

- Agents can propose, rank, and explain only.
- Agents cannot trade, promote, change live config, or tune a running trial.
- Variant proposals come from the fixed diagnostics catalog.
- A rolling PASS is still only a candidate.
- Paper promotion requires human approval and untouched-data judgment.

## Exchange And Pair Coverage

The research loop can sweep multiple venues while execution V1 remains
single-exchange.

```mermaid
flowchart LR
    Env["RESEARCH_EXCHANGES and RESEARCH_SYMBOLS"] --> Targets["ResearchTarget list"]
    Targets --> Binance["binanceusdm symbols"]
    Targets --> Bybit["bybit symbols"]
    Targets --> Delta["delta symbols"]
    Binance --> SameGates["Same walk-forward gates"]
    Bybit --> SameGates
    Delta --> SameGates
    SameGates --> PairRank["Best lane per exchange/symbol"]
```

Default research universe:

- Exchanges: `binanceusdm`, `bybit`, `delta`.
- Symbols: `BTC/USDT:USDT`, `ETH/USDT:USDT`, `SOL/USDT:USDT`,
  `BNB/USDT:USDT`, `XRP/USDT:USDT`, `DOGE/USDT:USDT`.
- Timeframe: `1h`.

Runtime knobs:

```bash
RESEARCH_EXCHANGES=binanceusdm,bybit,delta
RESEARCH_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT
RESEARCH_SYMBOLS_BYBIT=BTC/USDT:USDT,SOL/USDT:USDT
RESEARCH_TIMEFRAME=1h
```

## Status Map

| Block | Status | Notes |
|---|---|---|
| Config, risk core, live gates | Current | Hard caps and live confirmation gates exist. |
| Candle/funding/OI ingestion | Current | Quality-gated public data ingestion. |
| Backtester and walk-forward gates | Current | OOS-only judgment, sparse/offensive gates. |
| Strategy registry and current lanes | Current | Funding MR, trend, offensive lanes. |
| Order manager, idempotency, WAL | Current | Timeout and reconciliation semantics exist. |
| Paper/shadow runner | Current | Uses same gateway/order manager path. |
| Dashboard read-only snapshot | Current | No control routes. |
| Multi-exchange research universe | Branch | Offline research only; execution remains V1 scoped. |
| Bounded edge research agents | Branch | Propose/rank/explain only. |
| Control-room dashboard cockpit | Branch | Visual architecture/status surface. |
| Scalper microstructure foundation | Branch | In-process features/risk/tick-stop foundation. |
| Scalper scanners and edge miner | Branch | Discover all derivative pairs; rank lanes by liquidity, PF, route cost, fill evidence, sample sufficiency, and microstructure hypothesis expectancy. |
| Live Binance testnet execution | Next | Required before any live mode. |
| Private stream reconciliation | Next | Source of truth for orders/fills/positions. |
| L2 order book builder | Current | Recorder writes L2 shards with L1 aliases for replay. |
| Tick-level stop monitoring | Next | Reduce-only exits through gateway. |
| Delta/Bybit live adapters | Next | After one venue is proven. |
| TimescaleDB historian | Deferred | Parquet is enough for V1. |
| NATS/shared-memory IPC | Deferred | Single-process V1 remains simpler and safer. |
| ONNX C-API hot path | Deferred | Only after model edge and latency need are proven. |
| Sub-3ms execution target | Deferred | Network RTT dominates this VPS-style system. |

## Promotion Flow

```mermaid
flowchart LR
    Exploratory["Exploratory research"] --> RollingPass["Rolling PASS candidate"]
    RollingPass --> PreReg["Human pre-registration"]
    PreReg --> Untouched["Untouched-data judgment"]
    Untouched -->|PASS| PaperApproval["Human paper approval"]
    Untouched -->|REJECT| Archive["Archive / diagnose"]
    PaperApproval --> PaperTrial["Paper trial"]
    PaperTrial --> Shadow["Shadow mode"]
    Shadow --> LiveSmall["live_small"]
    LiveSmall --> LiveFull["live_full"]
```

No branch in this flow is automatic. Every promotion step is gated by evidence,
human approval, and the execution safety layer.
