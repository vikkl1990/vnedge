"""Research-to-shadow manifest generation."""

import json

from vnedge.research.shadow_manifest import (
    build_shadow_lane_manifest,
    write_shadow_lane_manifest,
)


def test_shadow_manifest_promotes_only_locked_passes_to_ready_lanes(tmp_path):
    payload = {
        "generated_at": "2026-07-04T00:00:00+00:00",
        "results": [
            {
                "exchange": "binanceusdm",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "1h",
                "strategy": "funding_mean_reversion_v1",
                "verdict": "PASS",
                "selected_params": {"consensus": {"extreme_pct": 0.85}},
            },
            {
                "exchange": "bybit",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "1h",
                "strategy": "volatility_expansion_breakout_v1",
                "verdict": "PASS",
                "selected_params": {"consensus": {"breakout_bars": 48}},
            },
        ],
        "edge_agents": {
            "profitable_pairs": [
                {
                    "exchange": "binanceusdm",
                    "symbol": "BTC/USDT:USDT",
                    "timeframe": "1h",
                    "best_strategy": "funding_mean_reversion_v1",
                    "verdict": "PASS",
                    "oos_net_usd": 46.22,
                    "oos_trades": 21,
                    "gates": "sparse",
                },
                {
                    "exchange": "bybit",
                    "symbol": "BTC/USDT:USDT",
                    "timeframe": "1h",
                    "best_strategy": "volatility_expansion_breakout_v1",
                    "verdict": "PASS",
                    "oos_net_usd": 20.11,
                    "oos_trades": 17,
                    "gates": "offensive",
                },
                {
                    "exchange": "binanceusdm",
                    "symbol": "ETH/USDT:USDT",
                    "timeframe": "1h",
                    "best_strategy": "funding_mean_reversion_v1",
                    "verdict": "REJECT",
                    "oos_net_usd": 8.0,
                    "oos_trades": 12,
                    "gates": "sparse",
                },
            ],
        },
    }

    manifest = build_shadow_lane_manifest(payload)

    assert manifest["policy"]["can_trade"] is False
    assert manifest["policy"]["can_promote"] is False
    assert len(manifest["lanes"]) == 1
    lane = manifest["lanes"][0]
    assert lane["runtime_status"] == "ready"
    assert lane["mode"] == "shadow"
    assert lane["is_primary"] is True
    assert lane["strategy_id"] == "funding_mean_reversion_v1"
    assert lane["strategy_params"]["extreme_pct"] == 0.85
    assert len(manifest["blocked_candidates"]) == 2
    assert {b["reason"] for b in manifest["blocked_candidates"]} == {
        "strategy has no human-locked runtime params for shadow deployment",
        "rolling lane is profitable but has not passed gates",
    }

    path = tmp_path / "shadow_lanes.json"
    write_shadow_lane_manifest(manifest, path)
    assert json.loads(path.read_text())["lanes"][0]["lane_id"] == lane["lane_id"]
