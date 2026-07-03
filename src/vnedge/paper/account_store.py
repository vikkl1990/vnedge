"""Cross-session paper account persistence.

Real processes restart. Without persistence, every restart silently resets
the paper account to starting equity and the trial's evidence becomes a
patchwork of fresh starts. This store snapshots the account after every
processed bar (atomic tmp+rename, same crash-safety as the Parquet store)
and restores it on the next launch.

Scope (v1, honest): balance, open positions, and the tracker's risk
counters (peak equity, daily baseline, loss streak). NOT restored: resting
limit orders (the paper path uses market orders only) and historical fill
lists (the journal is the durable record of those).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from vnedge.paper.simulated_exchange import PaperPosition, SimulatedExchange
from vnedge.runtime.portfolio_tracker import PortfolioTracker

logger = logging.getLogger(__name__)


class PaperAccountStore:
    def __init__(self, path: Path | str, trial_id: str) -> None:
        self.path = Path(path)
        self.trial_id = trial_id

    def save_from(self, exchange: SimulatedExchange, tracker: PortfolioTracker) -> None:
        state = {
            "trial_id": self.trial_id,
            "saved_at": datetime.now(UTC).isoformat(),
            "starting_equity": tracker.starting_equity_usd,
            "balance_usd": exchange.balance_usd,
            "positions": [
                {"symbol": p.symbol, "quantity": p.quantity, "entry_price": p.entry_price}
                for p in exchange.get_positions()
            ],
            "tracker": tracker.export_state(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.path)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        state = json.loads(self.path.read_text())
        if state.get("trial_id") != self.trial_id:
            raise ValueError(
                f"account state at {self.path} belongs to trial "
                f"'{state.get('trial_id')}', not '{self.trial_id}' — refusing to mix trials"
            )
        return state

    def restore_into(
        self, exchange: SimulatedExchange, tracker: PortfolioTracker
    ) -> bool:
        """True if prior state existed and was restored."""
        state = self.load()
        if state is None:
            return False
        exchange.balance_usd = float(state["balance_usd"])
        exchange.positions = {
            p["symbol"]: PaperPosition(p["symbol"], float(p["quantity"]), float(p["entry_price"]))
            for p in state["positions"]
        }
        tracker.restore_state(state["tracker"])
        logger.info(
            "resumed paper account %s: balance $%.2f, %d position(s), loss streak %d",
            self.trial_id, exchange.balance_usd, len(exchange.positions),
            tracker.consecutive_losses,
        )
        return True
