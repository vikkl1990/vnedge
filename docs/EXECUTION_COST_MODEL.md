# Execution-cost model policy

VNEDGE separates known tariffs from uncertain execution economics:

```text
account fee card (maker/taker + verified Scalper/DETO modifiers + GST)
                         │ exact, versioned rules
                         ▼
at-send feature snapshot ──► P50/P90 execution residual (shadow first)
                         │
                         ▼
CostGate = verified tariff + max(P90, execution floor) + funding
           (unverified tariff cannot lower the generic profile)
```

The model predicts spread/slippage/impact/adverse-selection residuals. It never
predicts the exchange tariff, GST, or whether an account offer is active. DETO
is the Delta native-token discount; it is never inferred from price or model
output. Rates
in `vnedge.risk.fee_model` are repository reference cards, not a claim about a
current account; deployment must select a `schedule_id` backed by a statement.

## Static fallback profiles

| Profile | Fee assumption | Execution floor | Gate reserve | Taker RT gate |
|---|---:|---:|---:|---:|
| `swing` | 5 + 5 bps | 2 + 2 bps | 3 bps | 17.0 bps |
| `delta_swing` | (5 + 5) x 1.18 GST | 2 + 2 bps | 3 bps | 18.8 bps |
| `delta_scalp` | (5 + 5) x 1.18 GST | 3 + 3 bps | 2 bps | 19.8 bps |

The reserve is a pre-trade margin, not a booked venue expense. Realized model
costs exclude it: 14.0, 15.8, and 17.8 bps respectively before funding. A
verified account fee schedule may apply a qualifying Scalper close waiver;
static profiles never assume it.

## Safety contract

- Features are frozen at order send, timezone-aware, schema-versioned, and contain no future price.
- Labels require a real paper/live fill and the mid frozen at send.
- Training uses a chronological tail and embargo, never a random split.
- Research artifacts are shadow-only (`runtime_approved=False`).
- CostGate ignores an unapproved shadow prediction.
- A runtime-approved ML residual can only increase the execution floor; only an account-verified tariff card may lower the generic fee assumption.
- P90 is bounded below by the two-leg execution floor.
- Scalper and DETO default off. Active-but-unverified flags are ignored.
- Scalper Offer is a zero-fee close leg only when consent, symbol and hold window qualify.
- DETO multiplies only a nonzero charged leg; it never changes an already-waived close.
- When Scalper and DETO are both active, neither binds until stacking is account-verified.
- Discount verification carries a UTC expiry; stale state reverts to base fees.
- GST is applied after any verified modifier.
- Missing, invalid, future-dated, stale, schema-mismatched, or drifted models fall back to rules.
- An execution residual drift alarm raises the fallback floor and remains operator-visible.

## Implemented rollout

| Phase | State | Implementation |
|---|---|---|
| P0 | complete | leg-aware fees, conditional Scalper close waiver, DETO, GST, fixed execution floor |
| P1 | paper complete | simulated venue freezes `mid_at_send`; fill ledger records realized execution bps |
| P1 | live pending evidence | private fills stay unlabeled until the live submit path supplies frozen mid/features |
| P2 | research complete | dual HistGradientBoosting P50/P90 model with chronological OOS report |
| P3 | complete, opt-in | CostGate accepts only context-matched, `capital_safe` predictions; only verified tariff cards can replace generic fees |
| P4 | monitoring complete | ADWIN stream on realized execution cost minus frozen P50; no automatic promotion |

The quantile report's `shadow_gate_passed` is evidence for review only. It never
sets registry status, `tradeable`, capital eligibility, or order permission.
