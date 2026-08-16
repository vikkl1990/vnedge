# VNEDGE Architecture

The canonical, code-aligned architecture is [ARCHITECTURE_FLOW.md](ARCHITECTURE_FLOW.md).

Current operating posture:

- Public-data measurement lanes are the default product.
- The default capital roster is empty.
- Paper capital requires two explicit environment gates and a registered,
  capital-eligible strategy.
- Removed scanners, Pine pipelines, and automatic manifest/promotion paths are
  not part of the runtime.
- Live execution is unavailable as a deployment service. The guarded execution
  spine remains in source for safety testing and future reviewed work.
- Every order-capable path remains behind the shared risk gateway, durable WAL,
  reconciliation checks, and strategy eligibility gate.

This file intentionally stays short so architecture does not drift across two
competing documents.
