"""Canonical performance statistics shared by research and runtime reports."""

from __future__ import annotations


def profit_factor(gross_profit: float, gross_loss: float) -> float | None:
    """Return gross wins / gross losses, or ``None`` when it is undefined.

    A sample with no losing trades has no finite denominator. Representing that
    state as 999 or infinity makes thin samples sort like proven edge and also
    produces non-standard JSON. Callers must disclose it as undefined/∞ and
    keep their minimum-sample gate separate.
    """
    wins = max(0.0, float(gross_profit))
    losses = max(0.0, float(gross_loss))
    if losses <= 1e-12:
        return None
    return wins / losses
