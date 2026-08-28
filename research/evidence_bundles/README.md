# Research evidence bundles

This directory is the durable runtime location for content-addressed VNEDGE
research evidence. Bundle contents and the SQLite catalog are intentionally
gitignored; this README documents the contract only.

Each `veb_<sha256>/` directory contains:

- `manifest.json` — code, data, parameters, cost, engine and parity fingerprints;
- `report.json` — the canonical `vnedge.backtest_report.v1` payload.

Bundles are research-only and always encode `can_trade=false`,
`can_promote=false`, and `live_orders_enabled=false`. Verify or rebuild the
catalog with:

```bash
python -m vnedge.research.evidence_bundle verify --bundle research/evidence_bundles/veb_...
python -m vnedge.research.evidence_bundle reindex
```
