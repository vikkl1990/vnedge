import json
from datetime import UTC, datetime

from vnedge.dashboard.trade_journal import TradeJournalConfig, build_trade_journal


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_trade_journal_projects_scanner_chart_evidence_without_guessing(tmp_path):
    lane = "shadow_observe_structure_bos_1h_binanceusdm_btc_usdt_usdt_1h"
    write_jsonl(
        tmp_path / f"{lane}.journal.jsonl",
        [
            {
                "ts": "2026-08-20T12:00:01+00:00",
                "kind": "lane_eval",
                "payload": {
                    "bar_ts": "2026-08-20T11:00:00+00:00",
                    "strategy_id": "structure_bos_1h",
                    "exchange": "binanceusdm",
                    "symbol": "BTC/USDT:USDT",
                    "timeframe": "1h",
                    "decision_price": 62_900.0,
                    "fired": False,
                    "skip_reason": "inside_structure",
                    "backfill": False,
                },
            },
            {
                "ts": "2026-08-20T13:00:01+00:00",
                "kind": "lane_eval",
                "payload": {
                    "bar_ts": "2026-08-20T12:00:00+00:00",
                    "strategy_id": "structure_bos_1h",
                    "exchange": "binanceusdm",
                    "symbol": "BTC/USDT:USDT",
                    "timeframe": "1h",
                    "decision_price": 62_900.0,
                    "fired": True,
                    "signal_reason": "bos_up_break_swing_high",
                    "signal": {
                        "side": "long",
                        "stop_price": 62_000.0,
                        "take_profit_price": 65_000.0,
                    },
                    "backfill": False,
                },
            },
            {
                "ts": "2026-08-20T13:00:02+00:00",
                "kind": "shadow_intent",
                "payload": {
                    "intent_key": "bos-1",
                    "approved": True,
                    "intent": {
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                        "quantity": 0.01,
                        "notional_usd": 630.0,
                        "strategy_id": "structure_bos_1h",
                    },
                    "signal_reason": "bos_up_break_swing_high",
                    "stop_price": 62_000.0,
                    "take_profit_price": 65_000.0,
                    "bar_ts": "2026-08-20T12:00:00+00:00",
                },
            },
            {
                "ts": "2026-08-20T15:00:02+00:00",
                "kind": "shadow_outcome",
                "payload": {
                    "intent_key": "bos-1",
                    "resolution": "target",
                    "side": "long",
                    "entry_price": 63_000.0,
                    "exit_price": 65_000.0,
                    "virtual_net_usd": 18.5,
                    "bars_held": 2,
                    "bar_ts": "2026-08-20T14:00:00+00:00",
                },
            },
            {
                "ts": "2026-08-20T16:00:01+00:00",
                "kind": "lane_eval",
                "payload": {
                    "bar_ts": "2026-08-20T15:00:00+00:00",
                    "strategy_id": "structure_bos_1h",
                    "exchange": "binanceusdm",
                    "symbol": "BTC/USDT:USDT",
                    "timeframe": "1h",
                    "fired": False,
                    "skip_reason": "no_confirmed_swing_pair",
                    "backfill": False,
                },
            },
        ],
    )
    snapshot = {"lanes": [{"lane_id": lane}]}

    payload = build_trade_journal(snapshot=snapshot, journal_dir=tmp_path, limit=50)
    events = payload["scanner_events"]

    assert {event["kind"] for event in events} == {"signal", "entry", "exit", "evaluation"}
    entry = next(event for event in events if event["kind"] == "entry")
    assert entry["price"] == 63_000.0
    assert entry["stop_price"] == 62_000.0
    assert entry["target_price"] == 65_000.0
    assert entry["decision_price"] == 62_900.0
    outcome = next(event for event in events if event["kind"] == "exit")
    assert outcome["symbol"] == "BTC/USDT:USDT"
    assert outcome["strategy_id"] == "structure_bos_1h"
    assert outcome["entry_ts"] == "2026-08-20T12:00:00+00:00"
    assert outcome["virtual_net_usd"] == 18.5
    waiting = next(event for event in events if event["kind"] == "evaluation")
    assert waiting["reason"] == "no_confirmed_swing_pair"


def test_scanner_evidence_excludes_measurement_and_projects_rejections(tmp_path):
    scanner = "shadow_observe_squeeze_binanceusdm_btc_5m"
    measurement = "measurement_binanceusdm_btc_1h"
    for lane in (scanner, measurement):
        write_jsonl(
            tmp_path / f"{lane}.journal.jsonl",
            [{
                "ts": "2026-08-20T13:05:02+00:00",
                "kind": "lane_eval",
                "payload": {
                    "bar_ts": "2026-08-20T13:00:00+00:00",
                    "strategy_id": "squeeze_expansion_breakout_v3",
                    "symbol": "BTC/USDT:USDT",
                    "timeframe": "5m",
                    "decision_price": 63_125.0,
                    "fired": True,
                    "signal": {"side": "long"},
                },
            }],
        )
    with (tmp_path / f"{scanner}.journal.jsonl").open("a") as handle:
        handle.write(json.dumps({
            "ts": "2026-08-20T13:05:03+00:00",
            "kind": "cost_rejected",
            "payload": {
                "bar_ts": "2026-08-20T13:00:00+00:00",
                "strategy_id": "squeeze_expansion_breakout_v3",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "5m",
                "side": "long",
                "decision_price": 63_125.0,
                "reason": "net edge below fee wall",
            },
        }) + "\n")
    snapshot = {"lanes": [
        {"lane_id": scanner, "observation_class": "shadow_observe"},
        {"lane_id": measurement, "observation_class": "measurement"},
    ]}

    events = build_trade_journal(
        snapshot=snapshot, journal_dir=tmp_path, limit=50
    )["scanner_events"]

    assert {event["lane"] for event in events} == {scanner}
    rejected = next(event for event in events if event["kind"] == "rejection")
    assert rejected["source_event"] == "cost_rejected"
    assert rejected["decision_price"] == 63_125.0
    assert rejected["reason"] == "net edge below fee wall"


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
                "payload": {
                    "approved": True,
                    "path_id": "kernel_v1",
                        "execution_evidence": {
                            "decision_id": "dec_entry_1",
                            "strategy_id": "sats_5m_scalper_v1",
                            "htf_snapshot_id": "0123456789abcdef01234567",
                            "candle_source": "parquet",
                            "entry_clock": "next_5m_open",
                            "execution_contract_id": "kernel_v1|parquet|next_5m_open",
                        },
                },
            },
            {
                "ts": "2026-07-16T01:00:01+00:00",
                "kind": "order_intent",
                "payload": {
                    "path_id": "kernel_v1",
                    "client_order_id": "entry-1",
                        "execution_evidence": {
                            "decision_id": "dec_entry_1",
                            "strategy_id": "sats_5m_scalper_v1",
                            "htf_snapshot_id": "0123456789abcdef01234567",
                            "candle_source": "parquet",
                            "entry_clock": "next_5m_open",
                            "execution_contract_id": "kernel_v1|parquet|next_5m_open",
                        },
                    "intent": {
                        "symbol": "ETH/USD:USD",
                        "side": "long",
                        "quantity": 0.2,
                        "order_type": "market",
                        "reduce_only": False,
                    },
                },
            },
            {
                "ts": "2026-07-16T01:00:02+00:00",
                "kind": "order_submitted",
                "payload": {
                    "path_id": "kernel_v1",
                    "client_order_id": "entry-1",
                        "execution_evidence": {
                            "decision_id": "dec_entry_1",
                            "strategy_id": "sats_5m_scalper_v1",
                            "htf_snapshot_id": "0123456789abcdef01234567",
                            "candle_source": "parquet",
                            "entry_clock": "next_5m_open",
                            "execution_contract_id": "kernel_v1|parquet|next_5m_open",
                        },
                },
            },
            {
                "ts": "2026-07-16T01:00:03+00:00",
                "kind": "order_acknowledged",
                "payload": {
                    "path_id": "kernel_v1",
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
    assert payload["summary"]["actual_closed_trades"] == 1
    assert payload["summary"]["shadow_closed_trades"] == 2
    assert payload["summary"]["actual_realized_pnl_usd"] == 1.6
    assert payload["summary"]["fees_usd"] == 0.19
    assert payload["summary"]["actual_closed_net_usd"] == 1.41
    assert payload["summary"]["actual_closed_fees_usd"] == 0.19
    assert payload["summary"]["virtual_net_usd"] == 0
    assert payload["summary"]["performance_eligible_closed_trades"] == 1
    assert payload["orders"][0]["state"] == "acknowledged"
    assert payload["orders"][0]["decision_id"] == "dec_entry_1"
    assert payload["orders"][0]["permission_snapshot_id"] == (
        "0123456789abcdef01234567"
    )
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


def test_trade_journal_paginates_rows_without_changing_full_ledger_totals(tmp_path):
    write_jsonl(
        tmp_path / "alpha.journal.jsonl",
        [
            {
                "ts": f"2026-07-16T0{hour}:00:00+00:00",
                "kind": "shadow_outcome",
                "payload": {
                    "intent_key": f"intent-{hour}",
                    "resolution": "time_stop",
                    "virtual_net_usd": float(hour),
                },
            }
            for hour in range(1, 4)
        ],
    )

    first = build_trade_journal(
        snapshot={}, journal_dir=tmp_path, limit=2, offset=0
    )
    second = build_trade_journal(
        snapshot={}, journal_dir=tmp_path, limit=2, offset=2
    )

    assert first["summary"]["closed_trades"] == 3
    assert second["summary"]["closed_trades"] == 3
    assert first["summary"]["virtual_net_usd"] == 0
    assert second["summary"]["virtual_net_usd"] == 0
    assert len(first["closed_trades"]) == 2
    assert len(second["closed_trades"]) == 1
    assert first["page"]["has_more"] is True
    assert second["page"]["has_previous"] is True


def test_trade_journal_uses_full_stream_shadow_ledger_when_tail_lost_outcome(tmp_path):
    lane = "shadow_observe_squeeze_binanceusdm_btc_usdt_usdt_5m"
    journal = tmp_path / f"{lane}.journal.jsonl"
    rows = [
        {
            "ts": "2026-08-20T01:00:00+00:00",
            "kind": "shadow_outcome",
            "payload": {
                "intent_key": "old-resolved",
                "resolution": "stop",
                "virtual_net_usd": -12.3,
            },
        },
        *[
            {
                "ts": f"2026-08-30T12:{minute:02d}:00+00:00",
                "kind": "lane_eval",
                "payload": {"strategy_id": "squeeze_expansion_breakout_v4"},
            }
            for minute in range(50)
        ],
    ]
    write_jsonl(journal, rows)
    evidence = tmp_path / "scanner_evidence_latest.json"
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "generated_at": datetime.now(UTC).isoformat(),
                "source_window": {"complete": True},
                "resolved_trades_complete": True,
                "resolved_trades": [
                    {
                        "lane": lane,
                        "ts": "2026-08-20T01:00:00+00:00",
                        "kind": "shadow_outcome",
                        "strategy_id": "squeeze_expansion_breakout_v4",
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                        "resolution": "stop",
                        "entry_price": 100.0,
                        "exit_price": 99.0,
                        "virtual_net_usd": -12.3,
                        "fees_usd": 0.4,
                        "funding_usd": 0.1,
                        "intent_key": "old-resolved",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_trade_journal(
        snapshot={"lanes": [{"lane_id": lane}]},
        journal_dir=tmp_path,
        scanner_evidence_path=evidence,
        config=TradeJournalConfig(tail_bytes=256),
    )

    assert payload["summary"]["closed_trades"] == 1
    assert payload["summary"]["shadow_closed_trades"] == 1
    assert payload["summary"]["virtual_net_usd"] == 0
    assert payload["summary"]["shadow_execution_fees_usd"] == 0
    assert payload["summary"]["shadow_funding_usd"] == 0
    assert payload["summary"]["shadow_history_complete"] is True
    assert payload["summary"]["reconciliation_state"] == "matched"
    assert payload["closed_trades"][0]["source"] == "scanner_evidence_full_stream"


# --- cohort P&L split (honest headline) ------------------------------------------

from vnedge.dashboard.trade_journal import _cohort_pnl_rollup, _lane_cohort


def test_lane_cohort_classification():
    assert _lane_cohort("velocity_sats_5m_scalper_delta_india_eth_usd_usd_shadow") == "control"
    assert _lane_cohort("papertrial_stealth_trail_bbp_v1_delta_india_eth_usd_usd_4h") == "tracked"
    assert _lane_cohort("evidence_vnedge_algo_ml_pro_v1_delta_india_eth_usd_usd_4h_shadow") == "tracked"
    assert _lane_cohort("fee_wall_luxy_ut_bot_forecast_bybit_btc_usdt_usdt_15m_paper_probe") == "tracked"
    assert _lane_cohort("funding_mr_btc_v1_20260703") == "tracked"
    assert _lane_cohort("quant_signal_pack_v1_binanceusdm_ethusdt_shadow") == "research"
    assert _lane_cohort("") == "research"


def test_cohort_rollup_separates_controls_from_tracked():
    trades = [
        {"lane": "velocity_sats_5m_scalper_delta_india_eth_usd_usd_shadow", "virtual_net_usd": -10.0, "performance_eligible": True},
        {"lane": "velocity_sats_5m_scalper_delta_india_btc_usd_usd_shadow", "virtual_net_usd": -5.0, "performance_eligible": True},
        {"lane": "funding_mr_btc_v1_20260703", "virtual_net_usd": 4.0, "performance_eligible": True},
        {"lane": "papertrial_stealth_trail_bbp_v1_delta_india_eth_usd_usd_4h", "virtual_net_usd": -1.0, "performance_eligible": True},
        {"lane": "quant_signal_pack_v1_binanceusdm_ethusdt_shadow", "virtual_net_usd": -3.0, "performance_eligible": True},
    ]
    roll = _cohort_pnl_rollup(trades)
    assert set(roll) == {"tracked", "research", "control"}
    assert roll["control"]["closed"] == 2 and roll["control"]["net_usd"] == -15.0
    assert roll["control"]["wins"] == 0 and roll["control"]["win_rate_pct"] == 0.0
    assert roll["tracked"]["closed"] == 2 and roll["tracked"]["net_usd"] == 3.0
    assert roll["tracked"]["wins"] == 1 and roll["tracked"]["win_rate_pct"] == 50.0
    assert roll["research"]["closed"] == 1 and roll["research"]["net_usd"] == -3.0
    # every cohort carries its human label + honesty note
    assert roll["control"]["label"] == "Deliberate controls"
    assert "EXPECTED to lose" in roll["control"]["note"]


def test_cohort_rollup_present_in_journal_summary(tmp_path):
    out = build_trade_journal(snapshot=None, journal_dir=tmp_path, history_path=tmp_path / "none")
    assert "cohort_pnl" in out["summary"]
    assert set(out["summary"]["cohort_pnl"]) == {"tracked", "research", "control"}


def test_execution_clock_cohorts_never_share_operator_headline(tmp_path):
    fill_rows = []
    journal_rows = []
    for index, (clock, minute) in enumerate((("quote_hold", 0), ("next_15m_open", 20))):
        entry_id = f"entry-{index}"
        contract_id = f"kernel_v1|parquet|{clock}"
        fill_rows.extend(
            [
                {
                    "ts": f"2026-08-28T01:{minute:02d}:00+00:00",
                    "mode": "paper",
                    "venue": "delta_india",
                    "strategy_id": "test_v1",
                    "symbol": "BTC/USD:USD",
                    "side": "buy",
                    "quantity": 1.0,
                    "price": 100.0,
                    "fee_usd": 0.1,
                    "realized_pnl_usd": 0.0,
                    "client_order_id": entry_id,
                    "hash": f"h-{index}-entry",
                },
                {
                    "ts": f"2026-08-28T01:{minute + 5:02d}:00+00:00",
                    "mode": "paper",
                    "venue": "delta_india",
                    "strategy_id": "test_v1",
                    "symbol": "BTC/USD:USD",
                    "side": "sell",
                    "quantity": 1.0,
                    "price": 101.0,
                    "fee_usd": 0.1,
                    "realized_pnl_usd": 1.0,
                    "client_order_id": f"exit-{index}",
                    "hash": f"h-{index}-exit",
                },
            ]
        )
        evidence = {
            "decision_id": f"decision-{index}",
            "strategy_id": "test_v1",
            "candle_source": "parquet",
            "entry_clock": clock,
            "execution_contract_id": contract_id,
        }
        journal_rows.extend(
            [
                {
                    "ts": f"2026-08-28T01:{minute:02d}:00+00:00",
                    "kind": "order_intent",
                    "payload": {
                        "path_id": "kernel_v1",
                        "client_order_id": entry_id,
                        "execution_evidence": evidence,
                        "intent": {
                            "symbol": "BTC/USD:USD",
                            "side": "long",
                            "quantity": 1.0,
                            "notional_usd": 100.0,
                            "leverage": 1.0,
                        },
                    },
                },
                {
                    "ts": f"2026-08-28T01:{minute:02d}:01+00:00",
                    "kind": "order_submitted",
                    "payload": {
                        "path_id": "kernel_v1",
                        "client_order_id": entry_id,
                        "execution_evidence": evidence,
                    },
                },
            ]
        )
    write_jsonl(tmp_path / "alpha.fills.jsonl", fill_rows)
    write_jsonl(tmp_path / "alpha.journal.jsonl", journal_rows)

    payload = build_trade_journal(
        snapshot={"lane_id": "alpha"},
        journal_dir=tmp_path,
        lane="alpha",
    )

    summary = payload["summary"]
    assert summary["mixed_entry_clock_headline"] is True
    assert summary["headline_actual_closed_net_usd"] is None
    assert summary["performance_entry_clocks"] == ["next_15m_open", "quote_hold"]
    assert set(summary["execution_contract_pnl"]) == {
        "kernel_v1|parquet|next_15m_open",
        "kernel_v1|parquet|quote_hold",
    }
