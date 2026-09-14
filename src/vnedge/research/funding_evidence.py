"""Public funding evidence archive, without settlement or trading authority.

Delta's FUNDING candles describe a rate series, not an itemized settlement
ledger. Preserve the actual HTTP body for later verification; never mint an
ml_funding_coverage receipt from candles or from a websocket schedule rollover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from vnedge.ml.lab_pipeline import read_object, write_once
from vnedge.ml.ledger_labels import digest, money

BASE = "https://api.india.delta.exchange/v2/history/candles"


def fetch_history(symbol: str, start: int, end: int) -> bytes:
    if symbol not in {"BTCUSD", "ETHUSD"} or not 0 < end - start <= 86400:
        raise ValueError("unsupported_funding_scope")
    url = (
        BASE
        + "?"
        + urlencode({"symbol": "FUNDING:" + symbol, "resolution": "1h", "start": start, "end": end})
    )
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=15) as response:
        if response.geturl().split("?")[0] != BASE:
            raise ValueError("funding_redirect_refused")
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("funding_response_too_large")
    return raw


def archive_history(root: Path, symbol: str, start: int, end: int, raw: bytes) -> dict:
    if symbol not in {"BTCUSD", "ETHUSD"} or not 0 < end - start <= 86400:
        raise ValueError("unsupported_funding_scope")
    if end > int(datetime.now(UTC).timestamp()) or len(raw) > 2_000_000:
        raise ValueError("unclosed_or_oversized_funding_window")
    payload = json.loads(raw)
    if payload.get("success") is not True or not isinstance(payload.get("result"), list):
        raise ValueError("funding_source_unsuccessful")
    rows = payload["result"]
    if len(rows) >= 2000:
        raise ValueError("funding_page_may_be_truncated")
    seen = set()
    for row in rows:
        stamp = row["time"]
        if type(stamp) is not int or not start <= stamp <= end or stamp in seen:
            raise ValueError("funding_history_identity_invalid")
        seen.add(stamp)
        for key in ("open", "high", "low", "close"):
            money(row[key])
    body = {
        "schema": "delta_funding_history_archive_v1",
        "exchange": "delta_india",
        "symbol": symbol,
        "start": start,
        "end": end,
        "resolution": "1h",
        "endpoint": BASE,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_response": raw.decode("utf-8"),
        "settlement_verified": False,
        "reason": "rate_candles_do_not_prove_settlements",
    }
    archive_id = digest(body)
    path = root / "archives" / f"{archive_id}.json"
    if path.exists():
        if read_object(path) != body:
            raise ValueError("funding_archive_conflict")
    else:
        write_once(path, body)
    return {
        "symbol": symbol,
        "archive_id": archive_id,
        "rows": len(rows),
        "history_archived": True,
        "settlement_verified": False,
        "reason": body["reason"],
        "start": start,
        "end": end,
    }


def collect_once(root: Path) -> dict:
    # Re-fetch the same completed window; identical responses deduplicate and
    # source revisions become new artifacts, never silent history edits.
    end = (int(datetime.now(UTC).timestamp()) // 3600 - 1) * 3600
    status = {
        "schema": "funding_evidence_status_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "markets": [],
        "can_trade": False,
        "can_promote": False,
    }
    for symbol in ("BTCUSD", "ETHUSD"):
        try:
            status["markets"].append(
                archive_history(
                    root, symbol, end - 86400, end, fetch_history(symbol, end - 86400, end)
                )
            )
        except Exception as exc:  # noqa: BLE001 — failed reads stay visible, never coverage
            status["markets"].append(
                {
                    "symbol": symbol,
                    "history_archived": False,
                    "settlement_verified": False,
                    "reason": str(exc),
                }
            )
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "status.json.tmp"
    temporary.write_text(json.dumps(status, allow_nan=False))
    temporary.replace(root / "status.json")
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/funding_evidence"))
    parser.add_argument("--interval-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.interval_seconds and args.interval_seconds < 300:
        parser.error("interval must be zero (once) or at least 300 seconds")
    while True:
        print(json.dumps(collect_once(args.root)), flush=True)
        if not args.interval_seconds:
            return
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
