"""Stable post-arm conversion rejection taxonomy."""

from __future__ import annotations

from collections.abc import Iterable

CONVERSION_REJECT_CATEGORIES = frozenset(
    {
        "regime_flat",
        "family_mismatch",
        "quote_missing",
        "hold_fail",
        "spread",
        "fee_floor",
        "min_net",
        "below_lot",
        "spec",
        "risk",
        "other",
    }
)


def _texts(values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        return (values.lower(),)
    if isinstance(values, Iterable):
        return tuple(str(value).lower() for value in values)
    return (str(values).lower(),) if values else ()


def conversion_reject_category(values: object) -> str:
    """Return exactly one operator category for one rejected intent."""
    checks = _texts(values)
    joined = " ".join(checks)
    if "regime_flat" in joined or "market_regime_not_ready" in joined:
        return "regime_flat"
    if "family_mismatch" in joined or "market_regime_playbook_blocked" in joined:
        return "family_mismatch"
    if any(token in joined for token in ("quote_missing", "book_stale", "invalid_quote")):
        return "quote_missing"
    if any(
        token in joined
        for token in (
            "hold_fail",
            "probe_failed",
            "chase_burn",
            "quote_buffer_overflow",
            "quote_ingest_lag",
            "quote_clock_skew",
            "quote_out_of_order",
        )
    ):
        return "hold_fail"
    if any(token in joined for token in ("spread_too_wide", "spread:")):
        return "spread"
    if any(
        token in joined
        for token in ("below_lot", "too_small", "minimum_size", "min_size", "size_step")
    ):
        return "below_lot"
    if any(
        token in joined
        for token in ("product_spec", "spec_drift", "tick_size", "contract_value")
    ):
        return "spec"
    if any(token in joined for token in ("min_net", "net_edge", "projected_net")):
        return "min_net"
    if any(
        token in joined
        for token in ("fee_floor", "cost_wall", "round_trip", "cost_gate:")
    ):
        return "fee_floor"
    if any(
        check.startswith(("sizing:", "shadow_portfolio:", "risk", "gateway:"))
        for check in checks
    ):
        return "risk"
    return "other"


def transition_reject_category(state: object) -> str | None:
    """Classify terminal/reset quote transitions; an active probe is not a reject."""
    value = str(state or "").lower()
    if not value or value in {
        "no_active_arm",
        "armed_long",
        "armed_short",
        "armed_both_sides",
        "long_probe",
        "short_probe",
        "long_accepted",
        "short_accepted",
    }:
        return None
    category = conversion_reject_category(value)
    return None if category == "other" else category


__all__ = [
    "CONVERSION_REJECT_CATEGORIES",
    "conversion_reject_category",
    "transition_reject_category",
]
