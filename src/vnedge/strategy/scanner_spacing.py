"""Shared signal-spacing policy for scanner registrations.

Only a setup that has passed every final eligibility gate may consume the
cooldown.  Keeping this rule in one helper prevents a raw pattern match from
silently suppressing a later, economically eligible setup.
"""

from __future__ import annotations

import pandas as pd


def apply_final_eligibility_spacing(
    long_eligible: pd.Series,
    short_eligible: pd.Series,
    *,
    min_bars_between_signals: int,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return long fires, short fires, and spacing availability.

    The input series must already include data-quality, session, structure,
    liquidity/volume, and projected-edge gates.  Rejected candidates never
    advance ``last_fire``.
    """
    if min_bars_between_signals < 1:
        raise ValueError("min_bars_between_signals must be positive")
    if len(long_eligible) != len(short_eligible):
        raise ValueError("long and short eligibility must have equal length")
    if not long_eligible.index.equals(short_eligible.index):
        raise ValueError("long and short eligibility indexes must match")

    fire_long = [False] * len(long_eligible)
    fire_short = [False] * len(long_eligible)
    spacing_ok = [False] * len(long_eligible)
    last_fire = -(10**9)
    pairs = zip(
        long_eligible.fillna(False).astype(bool),
        short_eligible.fillna(False).astype(bool),
        strict=True,
    )
    for position, (is_long, is_short) in enumerate(pairs):
        spacing_ok[position] = (
            position - last_fire >= min_bars_between_signals
        )
        if not spacing_ok[position]:
            continue
        if is_long:
            fire_long[position] = True
            last_fire = position
        elif is_short:
            fire_short[position] = True
            last_fire = position

    index = long_eligible.index
    return (
        pd.Series(fire_long, index=index, dtype=float),
        pd.Series(fire_short, index=index, dtype=float),
        pd.Series(spacing_ok, index=index, dtype=float),
    )


__all__ = ["apply_final_eligibility_spacing"]
