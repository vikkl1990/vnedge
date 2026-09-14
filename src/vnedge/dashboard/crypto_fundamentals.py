"""Point-in-time fundamentals dossier. Public measurements, never a trade score."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.request import Request, build_opener

from vnedge.dashboard.analyst_public import NoRedirect
from vnedge.dashboard.analyst_store import AnalystStore, digest, utc

VERSION = "crypto_fundamentals_v1"
DEFINITIONS = "https://docs.llama.fi/analysts/data-definitions"
# Explicit native-asset identity mappings; never match protocols by fuzzy ticker.
ASSETS = {
    "BTC": ("bitcoin", "proof_of_work_network"),
    "ETH": ("ethereum", "smart_contract_network"),
    "SOL": ("solana", "smart_contract_network"),
}
SYMBOLS = {base + quote: base for base in ASSETS for quote in ("USD", "USDT")}
TYPES = {
    "user_fees_usd": "dailyFees",
    "net_revenue_usd": "dailyRevenue",
    "token_holder_revenue_usd": "dailyHoldersRevenue",
}
FIELDS = {
    "user_fees_usd": "User-paid fees",
    "net_revenue_usd": "Revenue after supply-side allocation",
    "retained_protocol_revenue_usd": "Retained protocol revenue",
    "token_holder_revenue_usd": "Token-holder revenue / fee-funded burns",
    "emissions_usd": "Token emissions / incentives",
    "usage": "Economically meaningful usage",
    "circulating_supply": "Circulating supply",
    "unlocks": "Unlock schedule",
    "security_events": "Security and governance events",
    "corporate_earnings": "Corporate earnings",
}
CONTRACT_HASH = digest(
    [VERSION, ASSETS, TYPES, FIELDS, "availability is retrieval, never historical publication"]
)


def fetch_metric(slug: str, datatype: str) -> dict[str, Any]:
    if slug not in {a[0] for a in ASSETS.values()} or datatype not in TYPES.values():
        raise ValueError("unmapped_fundamental_source")
    url = f"https://api.llama.fi/summary/fees/{slug}?dataType={datatype}"
    with build_opener(NoRedirect).open(
        Request(url, headers={"Accept": "application/json"}), timeout=15
    ) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("fundamentals_response_bound")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise TypeError("fundamentals_response_invalid")
    return result


def normalize_metric(
    payload: dict[str, Any], base: str, metric: str, received: datetime
) -> dict[str, Any]:
    slug = ASSETS[base][0]
    if (
        metric not in TYPES
        or payload.get("id") != "chain#" + slug
        or payload.get("slug") != slug
        or payload.get("protocolType") != "chain"
    ):
        raise ValueError("fundamentals_identity_mismatch")
    rows = payload.get("totalDataChart")
    if not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError("fundamental_chart_bound")
    values = {}
    for stamp, value in rows:
        if isinstance(stamp, bool) or isinstance(value, bool):
            raise TypeError("fundamental_boolean")
        epoch, number = float(stamp), float(value)
        if not math.isfinite(epoch) or epoch % 86400 or not math.isfinite(number):
            raise ValueError("fundamental_daily_point_invalid")
        start = datetime.fromtimestamp(epoch, UTC)
        if start + timedelta(days=1) > received:
            continue
        if epoch in values and values[epoch] != number:
            raise ValueError("fundamental_conflicting_daily_point")
        values[epoch] = number
    if not values:
        raise ValueError("no_completed_fundamental_day")
    last = max(values)
    start = datetime.fromtimestamp(last, UTC)
    source = f"https://api.llama.fi/summary/fees/{slug}?dataType={TYPES[metric]}"
    # No growth claim from a single observation, no fees -> earnings substitution.
    return {
        "schema": VERSION,
        "asset": base,
        "metric": metric,
        "value": values[last],
        "unit": "USD",
        "period_start": start.isoformat(),
        "period_end": (start + timedelta(days=1)).isoformat(),
        "received_at": received.isoformat(),
        "source_url": source,
        "definitions_url": DEFINITIONS,
        "provider_identity": payload["id"],
        "max_age_seconds": 3 * 86400,
        "contract_hash": CONTRACT_HASH,
        "historical_backtest_eligible": False,
        "can_trade": False,
        "methodology": str(
            payload.get("methodology", {}).get(
                {
                    "user_fees_usd": "Fees",
                    "net_revenue_usd": "Revenue",
                    "token_holder_revenue_usd": "HoldersRevenue",
                }[metric],
                "Provider definition; attribution requires verification",
            )
        )[:2000],
    }


def collect_fundamentals(store: AnalystStore) -> dict[str, Any]:
    now = datetime.now(UTC)
    previous = store.read("fundamentals_collection", "native_assets", now=now)
    if previous and (now - utc(previous[0]["available_at"])).total_seconds() < 6 * 3600:
        return {"status": "not_due", "issues": []}
    status: dict[str, Any] = {"status": "collected", "observations": 0, "issues": []}
    for base, (slug, _) in ASSETS.items():
        for metric, datatype in TYPES.items():
            if base == "BTC" and metric != "user_fees_usd":
                continue
            scope = base + "/" + metric
            try:
                raw = fetch_metric(slug, datatype)
                received = datetime.now(UTC)
                ref = store.append("fundamental_raw", scope, raw, received)
                body = normalize_metric(raw, base, metric, received)
                body["raw_ref"] = ref
                store.append("fundamental", scope, body, received)
                status["observations"] += 1
            except Exception as exc:  # noqa: BLE001 - failed reads stay visible, never zero
                status["issues"].append(scope + ":" + type(exc).__name__)
    status["generated_at"] = datetime.now(UTC).isoformat()
    store.append("fundamentals_collection", "native_assets", status, utc(status["generated_at"]))
    return status


def fundamentals_dossier(store: AnalystStore, symbol: str, now: datetime) -> dict[str, Any]:
    base = SYMBOLS.get(symbol)
    fields, evidence = [], []
    for metric, label in FIELDS.items():
        row: dict[str, Any] = {
            "metric": metric,
            "label": label,
            "status": "missing",
            "value": None,
            "reason": "No verified source observation",
            "source_url": None,
        }
        if base and (
            metric == "corporate_earnings"
            or base == "BTC"
            and metric
            in (
                "retained_protocol_revenue_usd",
                "net_revenue_usd",
                "token_holder_revenue_usd",
                "unlocks",
            )
        ):
            row.update(
                status="not_applicable",
                reason="Native network, not corporate equity or a governance-token vesting claim",
            )
        elif not base:
            row.update(
                reason="Asset-type and protocol identity mapping required; no fuzzy ticker matching"
            )
        elif metric in TYPES:
            try:
                records = store.read("fundamental", base + "/" + metric, now=now)
                if records:
                    record, body = records[0], records[0]["body"]
                    received, end = utc(body["received_at"]), utc(body["period_end"])
                    if (
                        body["asset"] != base
                        or body["metric"] != metric
                        or body["contract_hash"] != CONTRACT_HASH
                        or not math.isfinite(body["value"])
                    ):
                        raise ValueError("invalid_fundamental_record")
                    fresh = (
                        end <= received <= now
                        and (now - end).total_seconds() <= body["max_age_seconds"]
                        and (now - received).total_seconds() <= body["max_age_seconds"]
                    )
                    row.update(
                        body,
                        status="current" if fresh else "stale",
                        value=body["value"] if fresh else None,
                        reason="Recorded at retrieval; not a historical as-of feature",
                        evidence_id=record["evidence_id"],
                    )
                    evidence.append(
                        {
                            "id": record["evidence_id"],
                            "kind": "fundamental",
                            "as_of": body["period_end"],
                            "state": row["status"],
                            "source": body["source_url"],
                            "summary": label,
                        }
                    )
            except Exception:  # noqa: BLE001
                row.update(status="invalid", reason="Fundamental evidence failed validation")
        fields.append(row)
    body = {
        "version": VERSION,
        "contract_hash": CONTRACT_HASH,
        "asset": base,
        "template": ASSETS[base][1] if base else "unmapped_asset",
        "fields": fields,
        "health": "not_assessed",
        "evidence": evidence,
        "can_trade": False,
        "can_promote": False,
        "note": "Fees, net revenue, retained revenue, token-holder value and emissions are different measures. Revenue and its attributions must not be summed. Positioning and liquidity are separate. Missing is not zero.",
    }
    body["dossier_id"] = digest(body)
    return body
