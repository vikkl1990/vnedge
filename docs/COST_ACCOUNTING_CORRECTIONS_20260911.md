# Cost accounting corrections — 2026-09-11

Implemented locally; no roster, strategy, capital, or deployment changes.

## Versioned estimate correction

`delta_scalp_v2` is an opt-in cost profile. Its shared execution calculation
uses max(entry impact, maker adverse-selection floor) for a maker entry and
the configured taker impact for the protective exit. This is a conservative
assumption, not a measurement of fill quality. Research, SessionCosts and
CostGate agree on this new profile.

| Profile / route | Research estimate | CostGate estimate | Gate + reserve | Approval gross floor |
| --- | ---: | ---: | ---: | ---: |
| delta_scalp / maker-taker (legacy) | 14.26 | 12.76 | 14.76 | 16.76 |
| delta_scalp_v2 / maker-taker | 14.26 | 14.26 | 16.26 | 18.26 |
| delta_scalp_v2 / taker-taker | 17.80 | 17.80 | 19.80 | 21.80 |

All figures are bps under the generic full tariff, no funding prediction,
and CostGate's default 4-bps minimum net edge. Reserve is 2 bps, not a fee.
The approval threshold is cost + minimum net; do not confuse it with the
displayed cost + reserve. Neither changes in this patch.

Legacy IDs deliberately retain their previous numerical outputs. They do NOT
become internally consistent retroactively. A strategy migrating to v2 needs
its own reviewed contract/replay; operational defaults remain unchanged.
No historical artifact is overwritten or reinterpreted as v2.

CostGate outputs now carry `cost_basis=pretrade_estimate`, an explicit
`estimated_execution_bps`, profile ID, execution policy and config SHA256.
The old `booked_execution_bps` field is a compatibility alias, NOT evidence
of venue charges. These fields are copied through CostDecisionEvidence into
the existing execution journal envelope, not added to OrderIntent.
New closed-bar replay artifacts and ScannerTrade rows are
explicitly `research_estimate` and include profile/hash attribution. The
legacy replay `net_bps_semantics=booked_execution` describes the old
reserve-excluded arithmetic, not actual venue settlement.

## Cash-first accounting

Backtest and paper per-fill fees now call the same Decimal cash function:

`fee_cash = abs(executed_notional) * all_in_fee_bps / 10000`

Existing float APIs remain float at their boundary; the calculation is Decimal
internally. This is not an end-to-end Decimal ledger migration. Tiny float
rounding differences versus historical runs are possible; old artifacts stay
unchanged. Account-aware FeeCalculation exposes `estimated_cash(notional)`;
tax is already included in its rate. Never multiply GST a second time.

`vnedge.plan.cash_costs.ChargedFill` and `reconcile_cash_fills` provide a
separate, read-only reconciliation path for normalized linear-contract fills:

- Actual or simulated fill prices determine gross cash. No modeled spread,
  slippage, adverse-selection or safety reserve is subtracted again.
- Each partial execution carries its own quantity, price and fee. Identical
  fill retries dedupe; conflicting duplicate IDs fail.
- Fees including tax must not also supply a separate tax charge. Fees
  excluding tax must supply it explicitly (zero allowed; missing rejected).
- Rebates and funding are signed cash. Unknown funding leaves final net/PnL
  bps unset, while net-before-funding remains available.
- Account, venue, decision, symbol, currency and actual/simulation basis may
  not be mixed. Non-flat inventory is rejected instead of pretending a cash
  flow is realized PnL. This audit does not replace the portfolio ledger.
- Contract quantity is converted to base explicitly once, using an evidenced
  multiplier. There is no inferred multiplier, leverage charge or FX rate.
- Charge currency must equal the normalized quote currency. INR/USDT/USD
  conversions require separate evidenced normalization, not a guessed rate.

Example: 0.01 BTC bought at 100,000 and sold at 100,200, with reported all-in
fees 0.59 and 0.59118 in the same quote currency, gives gross 2, charges
1.18118, net-before-funding 0.81882. That is 8.1882 bps of entry notional.
This is a synthetic arithmetic fixture, not an actual account fill.

For a normalized JSON statement with `fills` matching ChargedFill fields,
optional `funding_paid` and its `funding_source_ref`:

```sh
PYTHONPATH=src .venv/bin/python -m vnedge.plan.cash_costs /absolute/path/statement.json
```

Outputs JSON and the input SHA256; never writes the source or operational
book. A source reference is attribution, not validation: the report explicitly
does not claim the supplied account statement has been independently verified.

## Discount safety

Generic Delta profiles still assume full tariff. Verified account schedules
retain their consent, symbol, hold-window and expiry checks. A discount call
without an explicit evaluation timestamp now falls back to full tariff with
`discount_evaluation_time_missing`; otherwise expiry could be bypassed.
Runtime hybrid predictions already provide the observation clock.

Official references rechecked 2026-09-11:

- [Delta fees](https://www.delta.exchange/fees): futures taker 0.05%, maker
  0.02%, plus 18% GST on fees, charged on notional.
- [Scalper Offer](https://www.delta.exchange/support/solutions?articleId=80001172745&categoryId=80000464980&folderId=80000723382): qualifying closing-leg waiver,
  consent before eligible trades, separate subaccount consent, hold limits,
  exclusions and offer-change conditions. It is not a blanket lower tariff.

Still unverified: this user's account/subaccount eligibility and actual
statement charges. No enrollment was attempted. Supply normalized fill and
fee/funding records before claiming cash reconciliation against Delta.

## Research consequence

The existing taker/taker OFI screen used 17.8 bps; v2 retains that estimate.
Changing the unit to cash therefore cannot change its sign. Maker/taker
research also retains 14.26 bps; the corrected new gate becomes stricter,
not cheaper. No new backtest or promotion is claimed by this accounting patch.

Tests cover profile parity, frozen legacy outputs, approval thresholds,
backtest profile hydration, per-fill fee math, partial fills/retries,
short/rebate/funding signs, tax/currency/identity rejection, and discount
expiry/consent/hold/clock behavior. Full pytest is required before handoff.

Final validation: `.venv/bin/python -m pytest -q` — **2,993 passed, 6 skipped**
in 172.01 seconds (611 dependency deprecation warnings). Targeted Ruff checks
and `git diff --check` passed. This validates the code and synthetic accounting
fixtures, not account eligibility, actual venue charges, or trading edge.
