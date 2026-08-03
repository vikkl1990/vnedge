from datetime import UTC, datetime

from vnedge.research.paper_only_survivor_registry import (
    ACTION_QUARANTINE_PAPER,
    ACTION_RUN_PAPER_MAKER_PROBE,
    ACTION_RUN_PAPER_ONLY,
    STATE_PAPER_MAKER_PROBE,
    STATE_PAPER_QUARANTINE,
    STATE_PAPER_SURVIVOR,
    PaperOnlySurvivorRegistryConfig,
    build_paper_only_survivor_registry,
)


def _grid(rows):
    return {
        "complete": True,
        "pre_registry": {"registry_id": "paper_only_survivor_prereg_v1"},
        "rows": rows,
    }


def _cell(
    strategy_id: str,
    *,
    n: int = 24,
    taker_net: float = 12.0,
    taker_pf: float = 1.7,
    taker_bps: float = 31.0,
    maker_net: float = 15.0,
    maker_pf: float = 2.0,
    maker_bps: float = 42.0,
):
    return {
        "strat": strategy_id,
        "exch": "binanceusdm",
        "sym": "BTC/USDT:USDT",
        "tf": "15m",
        "n": n,
        "taker": {
            "net": taker_net,
            "pf": taker_pf,
            "avg_net_bps": taker_bps,
            "win": 0.58,
            "dd": -3.0,
        },
        "maker": {
            "net": maker_net,
            "pf": maker_pf,
            "avg_net_bps": maker_bps,
            "win": 0.62,
            "dd": -2.0,
        },
        "exit_model": "active_exit_v1",
        "trail_atr_mult": 3.0,
    }


def test_registry_promotes_strict_taker_survivor_to_paper_roster():
    payload = build_paper_only_survivor_registry(
        grid=_grid([_cell("stealth_trail_bbp_v1")]),
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["survivor_state"] == STATE_PAPER_SURVIVOR
    assert row["action"] == ACTION_RUN_PAPER_ONLY
    assert row["selected_route"] == "taker"
    assert payload["summary"]["paper_survivors"] == 1
    assert payload["proposed_roster"]["paper_lanes"][0]["strategy_id"] == "stealth_trail_bbp_v1"
    assert payload["policy"]["paper_only"] is True
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_registry_routes_maker_only_edge_to_paper_maker_probe():
    payload = build_paper_only_survivor_registry(
        grid=_grid(
            [
                _cell(
                    "fvg_liquidity_breakout_v1",
                    taker_net=-1.0,
                    taker_pf=0.9,
                    taker_bps=-5.0,
                    maker_net=22.0,
                    maker_pf=2.2,
                    maker_bps=35.0,
                )
            ]
        ),
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["survivor_state"] == STATE_PAPER_MAKER_PROBE
    assert row["action"] == ACTION_RUN_PAPER_MAKER_PROBE
    assert row["selected_route"] == "maker"
    assert payload["summary"]["paper_maker_probes"] == 1
    assert payload["proposed_roster"]["maker_probes"][0]["lane_id"] == row["lane_id"]


def test_registry_quarantines_cells_that_fail_fee_wall_gates():
    payload = build_paper_only_survivor_registry(
        grid=_grid(
            [
                _cell(
                    "quant_signal_pack_v1",
                    n=40,
                    taker_net=-40.0,
                    taker_pf=0.4,
                    taker_bps=-35.0,
                    maker_net=2.0,
                    maker_pf=1.1,
                    maker_bps=3.0,
                )
            ]
        ),
        config=PaperOnlySurvivorRegistryConfig(min_avg_net_bps=25.0),
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    row = payload["rows"][0]
    assert row["survivor_state"] == STATE_PAPER_QUARANTINE
    assert row["action"] == ACTION_QUARANTINE_PAPER
    assert payload["summary"]["paper_quarantine"] == 1
    assert payload["proposed_roster"]["paper_lanes"] == []

