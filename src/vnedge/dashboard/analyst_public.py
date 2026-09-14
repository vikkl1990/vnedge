"""Public Delta observations: NOT canonical candles or lane acceptance evidence.

Bounded REST samples are descriptive only. They never prove quote-hold parity,
settled funding, full trade coverage, institution identity or profitable edge.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from vnedge.dashboard.analyst_store import AnalystStore, digest, utc

BASE = "https://api.india.delta.exchange/v2/"
SOURCE_DOC = "https://docs.delta.exchange/"
SYMBOL = re.compile(r"[A-Z0-9]{3,30}")
PRODUCT_CAP = 500


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise ValueError("public_redirect_refused")


def fetch(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if endpoint not in {"products", "tickers"} and not re.fullmatch(
        r"trades/[A-Z0-9]{3,30}", endpoint
    ):
        raise ValueError("public_endpoint_not_allowed")
    url = BASE + endpoint + ("?" + urlencode(params) if params else "")
    with build_opener(NoRedirect).open(
        Request(url, headers={"Accept": "application/json"}), timeout=15
    ) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("public_response_bound")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("public_response_unsuccessful")
    return payload


def number(value: Any, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise TypeError("numeric_bool_refused")
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
        raise ValueError("invalid_number")
    return value


def venue_time(value: Any) -> datetime:
    # Delta's public payloads use epoch seconds, milliseconds or microseconds.
    if isinstance(value, str) and "T" in value:
        return utc(value)
    epoch = number(value, positive=True)
    if epoch > 100_000_000_000_000:
        epoch /= 1_000_000
    elif epoch > 100_000_000_000:
        epoch /= 1000
    return datetime.fromtimestamp(epoch, UTC)


def product(row: dict[str, Any]) -> dict[str, Any]:
    symbol = row["symbol"]
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("invalid_product_symbol")
    base = row["underlying_asset"]["symbol"]
    if (
        row.get("contract_type") != "perpetual_futures"
        or row.get("state") != "live"
        or row.get("is_quanto") is not False
        or row.get("notional_type") != "vanilla"
        or row.get("contract_unit_currency") != base
        or row.get("quoting_asset", {}).get("symbol") != "USD"
    ):
        raise ValueError("unsupported_product_contract")
    result = {
        "symbol": symbol,
        "product_id": row["id"],
        "base": base,
        "quote": "USD",
        "contract_base": str(Decimal(str(row["contract_value"]))),
        "tick_size": str(Decimal(str(row["tick_size"]))),
        "trading_status": str(row.get("trading_status", "unknown")),
        "funding_interval_s": row.get("product_specs", {}).get("rate_exchange_interval"),
    }
    number(result["contract_base"], positive=True)
    number(result["tick_size"], positive=True)
    result["product_hash"] = digest(result)
    return result


def normalize_ticker(
    row: dict[str, Any], spec: dict[str, Any], received: datetime
) -> dict[str, Any]:
    if row.get("symbol") != spec["symbol"] or row.get("product_id") != spec["product_id"]:
        raise ValueError("ticker_product_identity_mismatch")
    stamp = venue_time(row["timestamp"])
    if stamp > received:
        raise ValueError("ticker_future_time")
    values: dict[str, Any] = {}
    issues = []
    for source, target in (
        ("oi_contracts", "open_interest_contracts"),
        ("oi_value_usd", "open_interest_usd"),
        ("turnover_usd", "turnover_24h_usd"),
        ("mark_price", "mark_price"),
    ):
        try:
            values[target] = number(row[source], nonnegative=True)
        except (KeyError, ValueError, TypeError):
            values[target] = None
            issues.append(target + "_unavailable")
    # Rate is carried in the venue's documented/display percent convention,
    # not silently converted to settlement cash or annualized.
    try:
        values["indicative_funding_pct"] = number(row["funding_rate"])
    except (KeyError, ValueError, TypeError):
        values["indicative_funding_pct"] = None
        issues.append("indicative_funding_unavailable")
    try:
        quotes = row["quotes"]
        bid, ask = (
            number(quotes["best_bid"], positive=True),
            number(quotes["best_ask"], positive=True),
        )
        bs, ass = (
            number(quotes["bid_size"], nonnegative=True),
            number(quotes["ask_size"], nonnegative=True),
        )
        if bid >= ask:
            raise ValueError("crossed_or_locked_book")
        values.update(
            bid=bid,
            ask=ask,
            spread_bps=(ask - bid) / ((ask + bid) / 2) * 10000,
            bid_size_contracts=bs,
            ask_size_contracts=ass,
            bid_size_base=bs * float(spec["contract_base"]),
            ask_size_base=ass * float(spec["contract_base"]),
        )
    except (KeyError, ValueError, TypeError):
        issues.append("book_invalid_or_unavailable")
    return {
        "symbol": spec["symbol"],
        "product_hash": spec["product_hash"],
        "venue_ts": stamp.isoformat(),
        "received_at": received.isoformat(),
        "source": "delta_public_rest_ticker",
        "expires_after_s": 120,
        "values": values,
        "issues": issues,
        "settlement_verified": False,
        "lane_consumed": False,
        "can_trade": False,
    }


def normalize_flow(
    rows: list[dict[str, Any]], spec: dict[str, Any], received: datetime
) -> dict[str, Any]:
    if not rows or len(rows) > 1000:
        raise ValueError("trade_sample_bound")
    buys = sells = 0.0
    stamps = []
    for row in rows:
        if row.get("symbol", spec["symbol"]) != spec["symbol"]:
            raise ValueError("flow_product_mismatch")
        stamp = venue_time(row["timestamp"])
        if stamp > received:
            raise ValueError("flow_future_time")
        size = number(row["size"], positive=True) * float(spec["contract_base"])
        number(row["price"], positive=True)
        role = row.get("buyer_role")
        side = (
            "buy"
            if role in {"taker", "t"}
            else "sell"
            if role in {"maker", "m"}
            else row.get("side")
        )
        if side == "buy":
            buys += size
        elif side == "sell":
            sells += size
        else:
            raise ValueError("aggressor_classification_missing")
        stamps.append(stamp)
    return {
        "symbol": spec["symbol"],
        "product_hash": spec["product_hash"],
        "source": "delta_public_recent_trades",
        "received_at": received.isoformat(),
        "venue_ts": max(stamps).isoformat(),
        "window_start": min(stamps).isoformat(),
        "expires_after_s": 120,
        "sample_trades": len(rows),
        "buy_base": buys,
        "sell_base": sells,
        "buy_share_pct": buys / (buys + sells) * 100,
        "coverage_complete": False,
        "institution_identity": "unknown",
        "can_trade": False,
    }


def collect_public(
    store: AnalystStore, *, flow_symbols: tuple[str, ...] = ("BTCUSD", "ETHUSD")
) -> dict[str, Any]:
    """One bounded sweep. Transport errors are persisted as coverage gaps."""
    if len(flow_symbols) > 8 or any(not SYMBOL.fullmatch(s) for s in flow_symbols):
        raise ValueError("flow_universe_bound")
    now = datetime.now(UTC)
    status: dict[str, Any] = {"generated_at": now.isoformat(), "issues": [], "can_trade": False}
    specs: dict[str, Any] = {}
    raw_refs = []
    try:
        cursor = None
        seen = set()
        for _ in range(5):
            params = {"contract_types": "perpetual_futures", "states": "live", "page_size": 100}
            if cursor:
                params["after"] = cursor
            page = fetch("products", params)
            raw_refs.append(store.append("raw", "delta_india/products", page, datetime.now(UTC)))
            rows = page["result"]
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError("products_page_invalid")
            for row in rows:
                try:
                    spec = product(row)
                    if spec["symbol"] in specs and spec != specs[spec["symbol"]]:
                        raise ValueError("conflicting_product_identity")
                    specs[spec["symbol"]] = spec
                except (KeyError, TypeError, ValueError) as exc:
                    status["issues"].append("product_excluded:" + type(exc).__name__)
            cursor = page.get("meta", {}).get("after")
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("products_cursor_repeated")
            seen.add(cursor)
        captured = datetime.now(UTC)
        universe = {
            "generated_at": captured.isoformat(),
            "expires_after_s": 7200,
            "products": sorted(specs.values(), key=lambda r: r["symbol"]),
            "complete": not bool(cursor),
            "raw_refs": raw_refs,
            "issues": status["issues"].copy(),
            "source": "delta_public_products",
            "schema": "analyst_universe_v1",
        }
        if len(specs) > PRODUCT_CAP:
            raise ValueError("product_universe_bound")
        store.append("universe", "delta_india", universe, captured)
        status["products"] = len(specs)
    except Exception as exc:  # noqa: BLE001 - public boundary, no hidden fallback
        status["issues"].append("universe_fetch_failed:" + type(exc).__name__)
        specs = {}
    if specs:
        try:
            page = fetch("tickers", {"contract_types": "perpetual_futures"})
            captured = datetime.now(UTC)
            ref = store.append("raw", "delta_india/tickers", page, captured)
            if not isinstance(page["result"], list) or len(page["result"]) > 1000:
                raise ValueError("ticker_response_bound")
            seen_symbols: set[str] = set()
            for row in page["result"]:
                symbol = row.get("symbol")
                if symbol not in specs:
                    continue
                if symbol in seen_symbols:
                    raise ValueError("duplicate_ticker_symbol")
                seen_symbols.add(symbol)
                try:
                    body = normalize_ticker(row, specs[symbol], captured)
                    body["raw_ref"] = ref
                    store.append("conditions", "delta_india/" + symbol, body, captured)
                except (ValueError, KeyError, TypeError) as exc:
                    store.append(
                        "conditions",
                        "delta_india/" + symbol,
                        {
                            "symbol": symbol,
                            "issues": ["ticker_invalid:" + type(exc).__name__],
                            "received_at": captured.isoformat(),
                            "raw_ref": ref,
                        },
                        captured,
                    )
            status["tickers"] = len(seen_symbols)
        except Exception as exc:  # noqa: BLE001
            status["issues"].append("ticker_fetch_failed:" + type(exc).__name__)
        for symbol in flow_symbols:
            if symbol not in specs:
                continue
            try:
                page = fetch("trades/" + symbol)
                captured = datetime.now(UTC)
                ref = store.append("raw", "delta_india/trades/" + symbol, page, captured)
                rows = page["result"]
                rows = rows.get("trades") if isinstance(rows, dict) else rows
                body = normalize_flow(rows, specs[symbol], captured)
                body["raw_ref"] = ref
                store.append("flow", "delta_india/" + symbol, body, captured)
            except Exception as exc:  # noqa: BLE001
                status["issues"].append("flow_fetch_failed:" + symbol + ":" + type(exc).__name__)
    status["generated_at"] = datetime.now(UTC).isoformat()
    store.append("collector", "delta_india", status, utc(status["generated_at"]))
    return status
