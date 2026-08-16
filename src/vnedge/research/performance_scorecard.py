"""Truthful performance disclosures for the Research scorecard.

Profit factor, Sharpe, and DSR are properties of one concrete after-cost sample.
They must never borrow an aggregate sample count from unrelated venue/timeframe
cells. Thin samples are hidden instead of formatting sentinel PF values as edge.
"""

from __future__ import annotations

import math
from typing import Any

MIN_PERFORMANCE_SAMPLES = 30
DEFLATED_SHARPE_GATE = 0.95


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sample_count(summary: dict[str, Any], report: dict[str, Any]) -> tuple[int, str]:
    for field, label in (
        ("trades", "trades"),
        ("closed_trades", "trades"),
        ("routed", "after_cost_outcomes"),
        ("selected", "after_cost_outcomes"),
        ("opportunities", "after_cost_outcomes"),
    ):
        raw = summary.get(field)
        if raw is not None:
            try:
                return max(0, int(raw)), label
            except (TypeError, ValueError):
                return 0, label
    try:
        return max(0, int(report.get("opportunity_count") or 0)), "after_cost_outcomes"
    except (TypeError, ValueError):
        return 0, "after_cost_outcomes"


def performance_disclosure(
    summary: dict[str, Any],
    report: dict[str, Any],
    *,
    min_samples: int = MIN_PERFORMANCE_SAMPLES,
) -> dict[str, Any]:
    """Normalize one fee-aware evidence cell for safe API/UI presentation."""
    samples, sample_unit = _sample_count(summary, report)
    qualified = samples >= min_samples
    source_verdict = str(summary.get("verdict") or report.get("verdict") or "") or None

    raw_pf = _finite(summary.get("profit_factor"))
    no_loss_pf = raw_pf is not None and raw_pf >= 999.0
    if not qualified:
        profit_factor = None
        pf_display = "hidden"
        pf_reason = f"requires at least {min_samples} after-cost outcomes"
    elif no_loss_pf:
        profit_factor = None
        pf_display = "∞"
        pf_reason = "no losing outcome; numeric PF sentinel suppressed"
    else:
        profit_factor = raw_pf
        pf_display = f"{raw_pf:.2f}" if raw_pf is not None else "not reported"
        pf_reason = None if raw_pf is not None else "not reported"

    raw_sharpe = _finite(summary.get("sharpe_after_cost", summary.get("sharpe")))
    sharpe_convention = str(
        summary.get("sharpe_convention")
        or report.get("sharpe_convention")
        or "not reported"
    )
    convention_known = sharpe_convention != "not reported"
    if not qualified:
        sharpe = None
        sharpe_reason = f"requires at least {min_samples} after-cost outcomes"
    elif raw_sharpe is None:
        sharpe = None
        sharpe_reason = "not reported"
    elif not convention_known:
        sharpe = None
        sharpe_reason = "annualization convention not reported"
    else:
        sharpe = raw_sharpe
        sharpe_reason = None

    raw_dsr = _finite(summary.get("deflated_sharpe"))
    raw_trials = _finite(summary.get("raw_trials", summary.get("n_trials")))
    effective_trials = _finite(
        summary.get("effective_trials", summary.get("effective_n_trials"))
    )
    deflated_sharpe = raw_dsr if qualified else None
    dsr_pass = bool(
        qualified
        and deflated_sharpe is not None
        and deflated_sharpe >= DEFLATED_SHARPE_GATE
    )

    return {
        "source_verdict": source_verdict,
        "verdict": source_verdict if qualified else "UNDER_SAMPLED",
        "metric_state": "SAMPLE_QUALIFIED" if qualified else "UNDER_SAMPLED",
        "sample_qualified": qualified,
        "samples": samples,
        "sample_unit": sample_unit,
        "min_samples": min_samples,
        "profit_factor": profit_factor,
        "profit_factor_display": pf_display,
        "profit_factor_reason": pf_reason,
        "sharpe": sharpe,
        "sharpe_reason": sharpe_reason,
        "sharpe_convention": sharpe_convention,
        "deflated_sharpe": deflated_sharpe,
        "deflated_sharpe_gate": DEFLATED_SHARPE_GATE,
        "deflated_sharpe_pass": dsr_pass,
        "raw_trials": raw_trials,
        "effective_trials": effective_trials,
        "trial_count_reason": (
            None
            if raw_trials is not None and effective_trials is not None
            else "raw and correlation-adjusted trial counts not both reported"
        ),
        "max_drawdown_pct": _finite(summary.get("max_drawdown_pct")) if qualified else None,
        "oos_net_bps": _finite(
            summary.get("oos_net_bps", summary.get("avg_selected_net_bps"))
        ),
        "metrics_after_cost": True,
    }


def scorecard_policy() -> dict[str, Any]:
    return {
        "min_samples": MIN_PERFORMANCE_SAMPLES,
        "sample_rule": "PF, Sharpe, and DSR use the same selected after-cost evidence cell",
        "profit_factor_basis": "gross positive net PnL / absolute gross negative net PnL",
        "sharpe_basis": "after-cost return series; value hidden when convention is absent",
        "deflated_sharpe_gate": DEFLATED_SHARPE_GATE,
        "trial_disclosure_rule": "report both raw N and correlation-adjusted N_eff",
        "ranking_rule": "sample-qualified rows first; never rank by PF alone",
    }
