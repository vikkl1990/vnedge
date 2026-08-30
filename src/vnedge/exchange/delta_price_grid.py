"""Delta India price-grid helpers using exact Decimal arithmetic."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal


def _decimal(value: Decimal | float | str) -> Decimal:
    parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("price/tick must be finite and positive")
    return parsed


def is_on_tick(price: Decimal | float | str, tick: Decimal | float | str) -> bool:
    p, t = _decimal(price), _decimal(tick)
    return p % t == 0


def snap_tick(
    price: Decimal | float | str,
    tick: Decimal | float | str,
    *,
    mode: str,
) -> Decimal:
    p, t = _decimal(price), _decimal(tick)
    rounding = {
        "floor": ROUND_FLOOR,
        "ceil": ROUND_CEILING,
        "nearest": ROUND_HALF_UP,
    }.get(mode)
    if rounding is None:
        raise ValueError("tick mode must be floor, ceil, or nearest")
    return (p / t).to_integral_value(rounding=rounding) * t


def snap_limit_entry(
    *, side: str, price: Decimal | float | str, tick: Decimal | float | str,
    post_only: bool,
) -> Decimal:
    if side == "buy":
        return snap_tick(price, tick, mode="floor" if post_only else "ceil")
    if side == "sell":
        return snap_tick(price, tick, mode="ceil" if post_only else "floor")
    raise ValueError(f"invalid side: {side}")


def snap_protective_stop(
    *, position_side: str, price: Decimal | float | str, tick: Decimal | float | str,
) -> Decimal:
    if position_side in {"long", "buy"}:
        return snap_tick(price, tick, mode="floor")
    if position_side in {"short", "sell"}:
        return snap_tick(price, tick, mode="ceil")
    raise ValueError(f"invalid position side: {position_side}")


def snap_take_profit(
    *, position_side: str, price: Decimal | float | str, tick: Decimal | float | str,
) -> Decimal:
    if position_side in {"long", "buy"}:
        return snap_tick(price, tick, mode="ceil")
    if position_side in {"short", "sell"}:
        return snap_tick(price, tick, mode="floor")
    raise ValueError(f"invalid position side: {position_side}")


def format_delta_price(price: Decimal | float | str) -> str:
    text = format(_decimal(price), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
