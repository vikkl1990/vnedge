from datetime import UTC, datetime

import pytest

from vnedge.exchange.delta_snapshot_validation import (
    DeltaSnapshotValidationError,
    DeltaSpecDriftMonitor,
    compare_to_baseline,
    parse_product_snapshot,
    validate_startup_universe,
)


def _raw(symbol: str) -> dict:
    return {
        "id": 27 if symbol == "BTCUSD" else 3136,
        "symbol": symbol,
        "contract_value": "0.001" if symbol == "BTCUSD" else "0.01",
        "tick_size": "0.5" if symbol == "BTCUSD" else "0.05",
        "initial_margin": "0.5",
        "maintenance_margin": "0.25",
        "contract_type": "perpetual_futures",
        "state": "live",
        "default_leverage": "200",
        "liquidation_penalty_factor": "0.5",
    }


def test_required_delta_products_validate_in_percent_units():
    snapshots = validate_startup_universe({s: _raw(s) for s in ("BTCUSD", "ETHUSD")})
    btc = snapshots["BTCUSD"]
    assert btc.max_implied_leverage == 200.0
    assert btc.to_contract_spec().maintenance_margin_pct == 0.25
    assert compare_to_baseline(btc) == []


def test_unknown_field_does_not_enter_sizing_and_missing_product_fails():
    raw = _raw("BTCUSD") | {"mystery_risk": "999"}
    snap = parse_product_snapshot(
        "BTCUSD", raw, fetched_at=datetime(2026, 8, 30, tzinfo=UTC)
    )
    assert not hasattr(snap, "mystery_risk")
    with pytest.raises(DeltaSnapshotValidationError, match="missing required product ETHUSD"):
        validate_startup_universe({"BTCUSD": raw})


def test_baseline_drift_blocks_startup():
    eth = _raw("ETHUSD") | {"tick_size": "0.10"}
    with pytest.raises(DeltaSnapshotValidationError, match="tick_size"):
        validate_startup_universe({"BTCUSD": _raw("BTCUSD"), "ETHUSD": eth})


class _Journal:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))
        return True


def test_drift_monitor_is_signal_and_does_not_mutate_frozen_spec():
    frozen = validate_startup_universe({s: _raw(s) for s in ("BTCUSD", "ETHUSD")})
    journal = _Journal()
    blocked = []

    def fetch(symbol):
        row = _raw(symbol)
        if symbol == "BTCUSD":
            row["tick_size"] = "1.0"
        return row

    monitor = DeltaSpecDriftMonitor(
        frozen, fetch_product=fetch, journal=journal, block_new_arms=blocked.append
    )
    drifts = monitor.check_once()
    assert [(d.symbol, d.field) for d in drifts] == [("BTCUSD", "tick_size")]
    assert monitor.arms_blocked and blocked == [True]
    assert frozen["BTCUSD"].tick_size == 0.5
    assert journal.rows[0][0] == "delta_product_spec_drift"
