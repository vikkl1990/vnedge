# Dynamic path-hold research policy

`vnedge.research.path_hold` is a pure, non-executable policy for the proposed
“hold while the next target remains reachable; exit on reversal” experiment.
It is intentionally not a 1-minute scanner and is not wired into any registered
strategy.

## Causal call order

```text
closed 4h/1h context
  -> registered entry rule
  -> CostGate
  -> SHADOW_OBSERVE virtual position
  -> ActiveExit stop / TP / max-hold decision
       -> decision: resolve through the normal reduce-only/virtual path
       -> no decision: evaluate_path_hold on the latest closed decision bar
            -> HOLD
            -> EXIT_REVERSAL
            -> EXIT_UNREACHABLE
            -> EXIT_UNAVAILABLE (forming, gap, bad/missing model input)
```

Ticks and forming 1-minute candles may update display state and trigger the
existing hard tick-stop adapter. They cannot produce a path-hold decision.

## Frozen inputs

A future strategy registration must declare:

- `max_target_distance_atr`;
- optional calibrated `min_hit_probability`;
- whether aligned HTF direction is mandatory;
- the decision timeframe and exact reversal event source.

The policy never estimates a probability. If a probability threshold is
configured, the caller must provide a causal, calibrated probability from a
sealed model; missing or invalid values return `EXIT_UNAVAILABLE`.

## Boundaries

- ActiveExit always runs first and remains authoritative for hard stops, TP
  ladders, fee-aware breakeven, trailing, and max holding.
- Targets cannot move. A target behind the current price is an integration
  fault, not permission to keep holding.
- The policy emits `PathDecision`, not `SignalIntent` or `OrderIntent`.
- `structure_bos_1h` remains unchanged because its pre-registration is frozen.
- Any adoption requires a new strategy ID, full costs, shadow evidence, and
  untouched out-of-sample evaluation.
