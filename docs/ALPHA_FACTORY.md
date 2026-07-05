# Alpha Factory

The alpha factory is a research-only slow-loop component. Its job is to turn
recorded tick/L2 tape into structural hypotheses that deserve replay, not to
produce executable signals.

## Flow

```mermaid
flowchart LR
    Tape["Recorded tick/L2 tape"] --> Mine["Mine structural hypotheses"]
    Mine --> Cost["Score after maker-first costs"]
    Cost --> Route["Route policy: blocked / maker / taker"]
    Route --> Queue["Replay queue"]
    Queue --> Replay["Conservative tick replay"]
    Replay --> Judgment["Untouched-data judgment"]
    Judgment --> Paper["Human-approved paper/shadow"]
```

Hard guards:

- `can_trade=false`
- `can_promote=false`
- raw hypotheses are not signals
- conservative replay is mandatory
- untouched judgment and human approval remain mandatory

## Families

V1 mines five structural families:

- `forced_flow_continuation`
- `absorption_reversal`
- `microprice_dislocation`
- `liquidity_vacuum_continuation`
- `volatility_impulse`

These are intentionally broader than the old imbalance scanner. They search
for contexts professional scalpers care about: forced flow, absorption,
thin-book continuation, volatility impulse, and microprice dislocation. A
family only enters the replay queue if conditional expectancy clears the
maker-first cost floor and route policy.

## Runtime

Continuous research publishes the payload under `alpha_factory` in
`research/live_research/latest.json`. Disable independently with:

```bash
ALPHA_FACTORY_ENABLED=0
```

Bound output rows with:

```bash
ALPHA_FACTORY_MAX_ROWS=50
```

Manual report:

```bash
python -m vnedge.research.alpha_factory --days 20260704
```

The factory shares the tick/L2 day selection used by scalper research, but it
does not depend on scalper research being enabled.
