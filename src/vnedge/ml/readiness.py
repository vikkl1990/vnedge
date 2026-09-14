"""Actionable research prerequisites, not trading or training permission."""
from __future__ import annotations

from typing import Any


def readiness_worklist(audit: dict[str, Any], pipeline: dict[str, Any]) -> list[dict[str, Any]]:
    funding = pipeline.get("funding_evidence") or {}
    markets = funding.get("markets") or []
    settlement = (funding.get("stale") is False and bool(markets)
                  and all(m.get("settlement_verified") is True for m in markets))
    labels = pipeline.get("ledger_bound_paper_labels", 0)
    plans = pipeline.get("plans_total", 0)
    counts = audit.get("counts") or {}
    items = [
        ("ledger", "Full accounting history", pipeline.get("ledger_coverage_complete") is True,
         "engineering", "Resolve per-lane chain, missing-ledger and orphan-fill errors; never rehash old evidence.",
         {"lanes": pipeline.get("ledger_sources", []), "exclusions": pipeline.get("ledger_exclusions", {})}),
        ("funding", "Settled funding evidence", settlement,
         "external_source", "Supply an authoritative settlement source and verify full coverage. Rate candles are insufficient.",
         {"markets": markets, "stale": funding.get("stale", True)}),
        ("features", "Exact pre-entry features", False,
         "evidence", "Verify each selected feature was available before its matched entry; do not fill unavailable feeds with zero.",
         {"feature_rows": counts.get("feature_rows", 0), "exact_matches": counts.get("exact_feature_matches", 0),
          "note": "A bounded audit does not certify dataset admission."}),
        ("labels", "Mature paper outcomes", False,
         "evidence", "Collect approved, reconciled paper episodes. No new paper lane is authorized by this checklist.",
         {"labels": labels, "train_floor": 200, "calibration_floor": 50, "holdout_floor": 50,
          "note": "Floors apply per exact cohort and registered split, not to pooled totals."}),
        ("plan", "Frozen experiment plans", plans > 0,
         "research", "Register exact features, cohort, cost hash and chronological splits before fitting; no automatic dates.",
         {"verified_plans": plans}),
        ("validation", "Holdout and forward economics", False,
         "evidence", "Run the frozen plan only after admission; compare after-cost holdout and prospective forward outcomes.",
         {"completed_research_runs": pipeline.get("runs_total", 0),
          "forward_reports": pipeline.get("predictions_total", 0)}),
    ]
    return [{"id": key, "title": title, "status": "RECORDED" if done else "PENDING",
             "owner": owner, "action": action, "evidence": evidence,
             "can_trade": False, "can_promote": False}
            for key, title, done, owner, action, evidence in items]
