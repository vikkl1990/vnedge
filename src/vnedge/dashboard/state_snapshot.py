"""State snapshot — the single, self-contained object the dashboard shows.

Built by the bot side and handed to the UI server as a plain dict. Every
snapshot is complete (docs/DESIGN.md §6): reconnecting clients never need
history, and a burst of market activity can never firehose the browser —
the UI only ever sees the latest coalesced state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.execution.order_state import OrderState
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.runtime.portfolio_tracker import PortfolioTracker


@dataclass(frozen=True)
class FeedHealth:
    exchange: str
    candles: str = "ok"
    funding: str = "ok"
    open_interest: str = "ok"
    last_update_ms: float = 0.0


def _risk_status(
    kill: KillSwitch, journal: DecisionJournal, om: OrderManager
) -> str:
    if kill.is_active:
        return "kill_switch_active"
    if not journal.available:
        return "journal_unavailable"
    if om.has_unresolved_orders:
        return "reconciling"
    return "ok"


def _last_risk_reject(om: OrderManager) -> str | None:
    last = None
    for order in om.orders.values():  # insertion-ordered
        if order.state is OrderState.RISK_REJECTED and order.history:
            last = order.history[-1].note
    return last


def build_snapshot(
    *,
    mode: str,
    live_trading_enabled: bool,
    tracker: PortfolioTracker,
    exchange: SimulatedExchange,
    kill_switch: KillSwitch,
    journal: DecisionJournal,
    order_manager: OrderManager,
    feed_health: FeedHealth,
    symbol: str = "",
    strategy_id: str = "",
    recent_alerts: list[dict] | None = None,
) -> dict:
    account = tracker.account_state()
    positions = []
    for pos in exchange.get_positions():
        bid, ask = exchange.quotes[pos.symbol]
        mark = (bid + ask) / 2.0
        positions.append(
            {
                "symbol": pos.symbol,
                "side": pos.side,
                "quantity": abs(pos.quantity),
                "entry_price": pos.entry_price,
                "mark_price": mark,
                "unrealized_usd": pos.quantity * (mark - pos.entry_price),
            }
        )
    open_orders = [
        {
            "client_order_id": s.client_order_id,
            "state": s.state,
            "requested_qty": s.requested_qty,
            "filled_qty": s.filled_qty,
        }
        for s in exchange.get_open_orders()
    ]
    return {
        "ts": datetime.now(UTC).isoformat(),
        "mode": mode,
        "symbol": symbol,
        "strategy_id": strategy_id,
        "recent_alerts": recent_alerts or [],
        "live_trading_enabled": live_trading_enabled,
        "kill_switch_active": kill_switch.is_active,
        "equity": tracker.equity_usd(),
        "peak_equity": tracker.peak_equity_usd,
        "realized_pnl": exchange.get_balances()["USDT"] - tracker.starting_equity_usd,
        "unrealized_pnl": tracker.unrealized_pnl_usd(),
        "daily_pnl": account.daily_pnl_usd,
        "consecutive_losses": account.consecutive_losses,
        "risk_status": _risk_status(kill_switch, journal, order_manager),
        "feed_health": {
            "exchange": feed_health.exchange,
            "candles": feed_health.candles,
            "funding": feed_health.funding,
            "open_interest": feed_health.open_interest,
            "last_update_ms": feed_health.last_update_ms,
        },
        "positions": positions,
        "open_orders": open_orders,
        "fills": len(exchange.get_fills()),
        "fees_usd": sum(f.fee_usd for f in exchange.get_fills()),
        "last_risk_reject": _last_risk_reject(order_manager),
        "last_journal_write": "ok" if journal.available else "unavailable",
    }
