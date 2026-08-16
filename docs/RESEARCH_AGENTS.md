# Continuous Research

The continuous-research service is measurement-first and evidence-only. It
ingests public candle and funding data, applies data-quality gates, evaluates
the small registered strategy set with walk-forward splits, and atomically
publishes an evidence artifact.

It does not:

- run scanner tournaments or Pine pipelines;
- generate an automatic shadow/paper manifest;
- change strategy eligibility or promotion state;
- create order intents or call an execution adapter;
- turn a favorable backtest into capital allocation.

The Docker service is opt-in:

```bash
docker compose --profile research up -d research-loop
```

The public measurement runtime is useful without this service. Research output
should be treated as a diagnostic input to human review, never as permission to
trade.
