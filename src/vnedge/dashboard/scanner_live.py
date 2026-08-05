"""Serve the local dashboard from live research scanner snapshots only.

This process has no exchange adapter, broker, order manager, credentials, or
execution route. It projects public-candle scanner health into the dashboard's
read-only state and scanner-tape endpoints.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.scanner_bridge import dashboard_scanner_payload

HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
TOKEN = os.environ.get("DASHBOARD_TOKEN", "vnedge-demo")
SCANNER_PATH = Path(
    os.environ.get(
        "DASHBOARD_SCANNER_PATH",
        "research/live_research/mtf_amf_rejection_scanner_latest.json",
    )
)


def read_scanner_payload(path: Path = SCANNER_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "generated_at": None,
            "scanner_id": "mtf_amf_rejection_scanner_v1",
            "symbols": {},
            "errors": {"scanner": "scanner snapshot unavailable"},
            "can_trade": False,
            "can_promote": False,
        }
    return payload if isinstance(payload, dict) else {}


def build_scanner_snapshot(payload: dict[str, Any], *, now: datetime | None = None) -> dict:
    current = now or datetime.now(UTC)
    bridge = dashboard_scanner_payload(payload, now=current)
    summary = bridge.get("summary") if isinstance(bridge.get("summary"), dict) else {}
    source_age_seconds = summary.get("source_age_seconds")
    feed_state = (
        "stale"
        if int(summary.get("stale") or 0) > 0
        else "error"
        if int(summary.get("errors") or 0) > 0
        else "ok"
    )
    rows = bridge.get("rows") if isinstance(bridge.get("rows"), list) else []
    lanes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        latest = row.get("latest_eval") if isinstance(row.get("latest_eval"), dict) else {}
        lanes.append(
            {
                "lane_id": f"scanner_{str(row.get('symbol') or '').lower()}",
                "mode": "shadow",
                "strategy_id": row.get("strategy_id"),
                "exchange": "delta_india",
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "feed": "ok" if row.get("state") not in {"DATA_STALE", "DATA_ERROR"} else "stale",
                "staleness_ms": float(row.get("source_age_seconds") or 0) * 1_000.0,
                "positions": 0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "price": None,
                "funding_rate": None,
                "last_eval": {
                    "fired": row.get("state") == "FIRING",
                    "side": latest.get("side"),
                    "signal_reason": row.get("why"),
                },
                "last_fired_ts": row.get("latest_eval_ts")
                if row.get("state") == "FIRING"
                else None,
                "funnel": {
                    "live_evals": 1,
                    "live_signals": 1 if row.get("state") == "FIRING" else 0,
                },
                "can_trade": False,
                "can_promote": False,
            }
        )

    return {
        "ts": current.isoformat(),
        "mode": "research scanner observation",
        "symbol": "BTCUSD,ETHUSD,SOLUSD",
        "strategy_id": str(payload.get("scanner_id") or "mtf_amf_rejection_scanner_v1"),
        "recent_alerts": [],
        "price": None,
        "funding_rate": 0.0,
        "session": {
            "scanner_source_generated_at": payload.get("generated_at"),
            "connected_symbols": summary.get("connected_symbols", 0),
            "firing": summary.get("firing", 0),
            "errors": summary.get("errors", 0),
        },
        "trial": None,
        "live_trading_enabled": False,
        "kill_switch_active": False,
        "equity": 0.0,
        "peak_equity": 0.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "daily_pnl": 0.0,
        "consecutive_losses": 0,
        "risk_status": "execution_locked_research_only",
        "feed_health": {
            "exchange": "delta_india public candles",
            "candles": feed_state,
            "funding": "not_used",
            "open_interest": "not_used",
            "last_update_ms": (
                float(source_age_seconds) * 1_000.0 if source_age_seconds is not None else None
            ),
        },
        "positions": [],
        "open_orders": [],
        "recent_fills": [],
        "fills": 0,
        "fees_usd": 0.0,
        "last_risk_reject": None,
        "last_journal_write": "scanner snapshot only",
        "lanes": lanes,
        "can_trade": False,
        "can_promote": False,
        "orders_sent": 0,
    }


async def main() -> None:
    provider = SnapshotProvider()
    provider.publish(build_scanner_snapshot(read_scanner_payload()))
    app = create_app(
        provider,
        token=TOKEN,
        snapshot_hz=2.0,
        realtime_scanner_path=SCANNER_PATH,
    )
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))

    async def publish_forever() -> None:
        while True:
            provider.publish(build_scanner_snapshot(read_scanner_payload()))
            await asyncio.sleep(2.0)

    print(f"VNEDGE scanner dashboard: http://{HOST}:{PORT}/?token={TOKEN}")
    await asyncio.gather(server.serve(), publish_forever())


if __name__ == "__main__":
    asyncio.run(main())
