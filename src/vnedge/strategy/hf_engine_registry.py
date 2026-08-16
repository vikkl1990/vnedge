"""Killed HF signal engines — post-mortem registry + trade-permission guard.

The fast ``SignalEngine`` world (tick/1s microstructure and hourly breakouts) was
investigated end-to-end on real Binance data in 2026-08. Every engine was KILLED:
the raw signals are frequently real, but the realized edge is smaller than the cost
of capturing it (the ~8bps taker cost wall) or, for passive quoting, smaller than
adverse selection (the maker wall). See docs/EDGE_INVESTIGATION_POSTMORTEM_20260816.

Policy — the same shape as the BaseStrategy registry's ``is_capital_eligible``:
the measurement code stays importable for research and backtesting; PERMISSION TO
TRADE is removed. The allowlist of tradeable engines is EMPTY (fail-closed): no
fast-tick engine may back a capital lane, and an unrecorded engine is barred until a
kill/promotion decision is written here. The tripwire test in
``tests/test_edge_kill_policy.py`` fails if a new ``SignalEngine`` subclass appears
without an entry, so a fast engine can never be silently re-introduced.

Promotion is intentionally hard: to make an engine tradeable it must first earn a
pre-registered OOS pass on untouched data through the promotion ladder, and only
then be added to ``TRADEABLE_HF_ENGINES`` — never by an in-place edit to an engine.
"""

from __future__ import annotations

# engine_id -> one-line post-mortem (the reason + the killing number/date).
KILLED_HF_ENGINES: dict[str, str] = {
    "OrderFlowImbalanceEngine": (
        "NO SIGNAL: 0/6901 candidates survive taker cost on real 1s BTC; realized "
        "-13.9bps ~= cost with ~0 gross drift = pure diffusion (2026-08-16)."
    ),
    "ShortTermMeanReversionEngine": (
        "INERT: fired 0x on real 1s data — the ranging + deviation gates never align "
        "at 1s resolution (2026-08-16)."
    ),
    "HourlyRangeBreakoutEngine": (
        "IS/OOS COLLAPSE: SEEN +3.76 -> UNTOUCHED -4.78bps over 124 trades at honest "
        "16bps cost; oracle (perfect-foresight exit) +9.89 but unharvestable; 5-min "
        "drift 10.8 -> 2.5 out-of-sample = sample-specific, not an edge (2026-08-16)."
    ),
    "RangeBreakoutEngine": (
        "BREAKOUT FAMILY (rolling-window variant) — same drift<cost failure as "
        "HourlyRangeBreakout; never produced an independent OOS pass (2026-08-16)."
    ),
    "CurrentHourBreakoutEngine": (
        "BREAKOUT FAMILY (current-hour variant) — same failure mode; never "
        "OOS-validated (2026-08-16)."
    ),
}

# Passive market-making was studied as a research SCRIPT (markout / adverse-selection
# analysis), never a committed engine. Recorded so nobody builds a PassiveMM engine
# expecting the spread to be free income.
PASSIVE_MM_POSTMORTEM = (
    "ADVERSE-SELECTION WALL: half-spread <=0.71bps < immediate adverse move ~0.5-1bps "
    "(negative markout from 0.5s on all 5 liquid symbols, BEFORE the ~2bps/side maker "
    "fee); the move is permanent/informed with no reversion to harvest (2026-08-16)."
)

# The allowlist of engines permitted to back a capital lane. EMPTY by design: the
# entire fast-tick world is killed. An engine earns a place here only via a
# pre-registered OOS pass through the promotion ladder.
TRADEABLE_HF_ENGINES: frozenset[str] = frozenset()


def is_hf_engine_tradeable(engine_id: str) -> bool:
    """True only for engines on the (currently empty) tradeable allowlist. Everything
    else — killed or simply unrecorded — is barred (fail-closed)."""
    return engine_id in TRADEABLE_HF_ENGINES


def killed_reason(engine_id: str) -> str | None:
    """The post-mortem for a killed engine, or None if it is not on the kill list."""
    return KILLED_HF_ENGINES.get(engine_id)
