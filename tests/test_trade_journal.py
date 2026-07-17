import json

from vnedge.dashboard.trade_journal import build_trade_journal


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_trade_journal_projects_fills_orders_and_virtual_trades(tmp_path):
    write_jsonl(
        tmp_path / "alpha.fills.jsonl",
        [
            {
                "ts": "2026-07-16T01:00:00+00:00",
                "mode": "paper",
                "venue": "delta_india",
                "strategy_id": "sats_5m_scalper_v1",
                "symbol": "ETH/USD:USD",
                "side": "buy",
                "quantity": 0.2,
                "price": 1780.0,
                "fee_usd": 0.09,
                "realized_pnl_usd": 0.0,
                "client_order_id": "entry-1",
                "hash": "h1",
            },
            {
                "ts": "2026-07-16T01:08:00+00:00",
                "mode": "paper",
                "venue": "delta_india",
                "strategy_id": "sats_5m_scalper_v1",
                "symbol": "ETH/USD:USD",
                "side": "sell",
                "quantity": 0.2,
                "price": 1788.0,
                "fee_usd": 0.1,
                "realized_pnl_usd": 1.6,
                "client_order_id": "exit-1",
                "hash": "h2",
            },
        ],
    )
    write_jsonl(
        tmp_path / "alpha.journal.jsonl",
        [
            {
                "ts": "2026-07-16T01:00:00+00:00",
                "kind": "risk_decision",
                "payload": {"client_order_id": "entry-1", "approved": True},
            },
            {
                "ts": "2026-07-16T01:00:01+00:00",
                "kind": "order_intent",
                "payload": {
                    "client_order_id": "entry-1",
                    "intent": {
                        "symbol": "ETH/USD:USD",
                        "side": "long",
                        "quantity": 0.2,
                        "order_type": "market",
                        "reduce_only": False,
                        "strategy_id": "sats_5m_scalper_v1",
                    },
                },
            },
            {
                "ts": "2026-07-16T01:00:02+00:00",
                "kind": "order_acknowledged",
                "payload": {
                    "client_order_id": "entry-1",
                    "exchange_order_id": "ex-1",
                },
            },
            {
                "ts": "2026-07-16T01:05:00+00:00",
                "kind": "shadow_outcome",
                "payload": {
                    "intent_key": "shadow-k",
                    "resolution": "target",
                    "virtual_net_usd": 2.25,
                    "side": "long",
                    "entry_price": 1781.0,
                    "exit_price": 1790.0,
                    "fees_usd": 0.2,
                    "bar_ts": "2026-07-16T01:05:00+00:00",
                },
            },
            {
                "ts": "2026-07-16T01:06:00+00:00",
                "kind": "scalp_shadow_outcome",
                "payload": {
                    "intent_key": "scalp-k",
                    "family": "cascade",
                    "resolution": "timeout",
                    "side": "short",
                    "virtual_net_usd": -0.4,
                    "taker_net_bps": -8.0,
                    "maker_net_bps": -3.0,
                    "entry_price": 1789.0,
                    "exit_price": 1790.0,
                },
            },
        ],
    )
    snapshot = {
        "lane_id": "alpha",
        "ts": "2026-07-16T01:09:00+00:00",
        "positions": [
            {
                "symbol": "ETH/USD:USD",
                "side": "long",
                "quantity": 0.2,
                "entry_price": 1780.0,
                "mark_price": 1788.0,
                "notional_usd": 357.6,
                "unrealized_usd": 1.6,
            }
        ],
        "open_orders": [],
        "session": {
            "trade_log": [
                {
                    "ts": "2026-07-16T01:00:00+00:00",
                    "event": "order_submitted",
                    "detail": "long ETH",
                }
            ]
        },
    }

    payload = build_trade_journal(
        snapshot=snapshot,
        journal_dir=tmp_path,
        history_path=tmp_path / "alpha.equity.jsonl",
        lane="alpha",
    )

    assert payload["policy"]["read_only"] is True
    assert payload["can_trade"] is False
    assert payload["summary"]["positions"] == 1
    assert payload["summary"]["fills"] == 2
    assert payload["summary"]["closed_trades"] == 3
    assert payload["summary"]["actual_realized_pnl_usd"] == 1.6
    assert payload["summary"]["fees_usd"] == 0.19
    assert payload["summary"]["virtual_net_usd"] == 1.85
    assert payload["orders"][0]["state"] == "acknowledged"
    assert {row["kind"] for row in payload["closed_trades"]} == {
        "actual_closing_fill",
        "shadow_outcome",
        "scalp_shadow_outcome",
    }
    assert any(event["event"] == "order_acknowledged" for event in payload["events"])


def test_trade_journal_days_filter_and_lane_filter(tmp_path):
    write_jsonl(
        tmp_path / "alpha.fills.jsonl",
        [
            {
                "ts": "2026-07-01T00:00:00+00:00",
                "symbol": "BTC/USDT:USDT",
                "side": "buy",
                "quantity": 0.01,
                "price": 100.0,
            }
        ],
    )
    write_jsonl(
        tmp_path / "beta.fills.jsonl",
        [
            {
                "ts": "2026-07-16T00:00:00+00:00",
                "symbol": "ETH/USD:USD",
                "side": "sell",
                "quantity": 0.02,
                "price": 1800.0,
            }
        ],
    )

    payload = build_trade_journal(
        snapshot={},
        journal_dir=tmp_path,
        lane="beta",
        since="2026-07-10T00:00:00+00:00",
    )

    assert payload["lane"] == "beta"
    assert payload["summary"]["fill_ledgers_scanned"] == 1
    assert [row["lane"] for row in payload["fills"]] == ["beta"]
