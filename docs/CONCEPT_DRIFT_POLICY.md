# Concept drift policy

VNEDGE separates three questions that are often incorrectly collapsed into one
“model drift” flag:

| Class | Meaning | Streaming inputs |
|---|---|---|
| Real drift | The relationship between setup/model and after-cost outcome changed | resolved binary error, log-loss, paper net bps |
| Cost drift | Execution economics changed | resolved realized fee bps |
| Virtual drift | The input distribution changed without proven outcome decay | closed-hour spread proxy, ATR percentile, volume rank |

The existing `vnedge.ml.robustness` PSI report remains the batch reference-vs-
current distribution check. `vnedge.ml.drift_supervisor` adds the streaming
layer using frozen River policies:

- ADWIN (`delta=0.002`) for error, log-loss, and spread streams;
- Page–Hinkley (`min_instances=30`, `delta=0.005`, `threshold=50`,
  `alpha=0.9999`) for realized cost and paper net-bps streams;
- KSWIN (`alpha=0.005`, 100-observation window, 30-observation statistic)
  for ATR-percentile and volume-rank distribution shifts.

## Data contracts

- Performance streams accept resolved after-cost outcomes only.
- Feature streams accept closed-bar measurements only.
- Forming bars, unrealized MFE, missing cost treatment, naive timestamps, and
  unregistered stream names are rejected.
- Each detector has a pre-registered warmup and cooldown.
- Detector or policy changes count as research trials in raw `N` and `N_eff`.

## Response contract

A detection produces an immutable `DriftEvent` with a classification,
detector parameters, evidence count, and recommended operator action. The event
is compatible with the existing alert JSONL timeline. The supervisor also
publishes an atomic read-only status artifact.

The supervisor itself always reports:

```text
alert_only = true
automatic_action = none
can_trade = false
can_promote = false
```

Recommendations such as reviewing a CostGate profile, marking feed quality
degraded, freezing a shadow update policy, or demoting a paper lane must be
executed through their existing reviewed risk/registry workflows. Drift never
rewrites a model, changes leverage, modifies strategy code, blocks reduce-only
exits, or promotes a replacement strategy.
