"""Edge leaderboard and promotion queue."""

from vnedge.research.edge_leaderboard import build_edge_leaderboard


def record(
    *,
    strategy="quant_signal_pack_v1",
    verdict="REJECT",
    net=10.0,
    trades=20,
    fees=5.0,
    pf=1.4,
    payoff=1.5,
    family_attribution=None,
    auto=False,
):
    payload = {
        "exchange": "binanceusdm",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "1h",
        "strategy": strategy,
        "verdict": verdict,
        "oos_net_usd": net,
        "oos_trades": trades,
        "total_fees_usd": fees,
        "profit_factor": pf,
        "payoff_ratio": payoff,
        "profitable_windows_pct": 70.0,
        "gates": "offensive",
        "auto": auto,
    }
    if family_attribution is not None:
        payload["family_attribution"] = family_attribution
    return payload


def test_passed_strategy_enters_judgment_queue_with_taker_route():
    board = build_edge_leaderboard([
        record(verdict="PASS", net=55.0, trades=30, fees=20.0, pf=2.0, payoff=2.2),
        record(strategy="trend_continuation_v1", net=-5.0, trades=40, pf=0.8),
    ])

    top = board["rows"][0]
    assert top["promotion_tier"] == "JUDGMENT_READY"
    assert top["route_decision"] == "TAKER_ALLOWED"
    assert top["can_trade"] is False
    assert top["requires_untouched_judgment"] is True
    assert board["promotion_queue"][0]["next_step"] == "pre_register_untouched_judgment"
    assert board["policy"]["can_promote"] is False


def test_quant_family_attribution_creates_isolated_variant_queue_item():
    board = build_edge_leaderboard([
        record(
            net=-12.0,
            trades=40,
            pf=0.9,
            family_attribution={
                "liquidity_sweep": {
                    "trades": 15,
                    "net_usd": 22.0,
                    "total_fees_usd": 9.0,
                    "profit_factor": 1.45,
                    "payoff_ratio": 1.7,
                    "win_rate_pct": 53.0,
                },
                "structure_break": {
                    "trades": 18,
                    "net_usd": -35.0,
                    "total_fees_usd": 8.0,
                    "profit_factor": 0.6,
                    "payoff_ratio": 0.8,
                    "win_rate_pct": 30.0,
                },
            },
        )
    ])

    family = next(r for r in board["rows"] if r["family"] == "liquidity_sweep")
    assert family["scope"] == "family"
    assert family["candidate_id"] == "quant_signal_pack_v1__liquidity_sweep_only"
    assert family["promotion_tier"] == "VARIANT_RESEARCH_READY"
    assert family["route_decision"] == "MAKER_ONLY"
    queue = next(q for q in board["promotion_queue"] if q["family"] == "liquidity_sweep")
    assert queue["next_step"] == "run_isolated_family_variant"


def test_negative_or_sub_pf_rows_are_blocked_not_queued():
    board = build_edge_leaderboard([
        record(net=12.0, trades=25, pf=1.05),
        record(strategy="trend_continuation_v1", net=-1.0, trades=25, pf=2.0),
    ])

    assert all(row["promotion_tier"] == "BLOCKED" for row in board["rows"])
    assert board["promotion_queue"] == []
    assert board["summary"]["blocked"] == 2


def test_auto_pass_requires_human_review_not_direct_judgment():
    board = build_edge_leaderboard([
        record(verdict="PASS", auto=True, net=35.0, trades=20, pf=1.5, payoff=2.0)
    ])

    assert board["rows"][0]["promotion_tier"] == "AUTO_PASS_REVIEW"
    assert (
        board["promotion_queue"][0]["next_step"]
        == "human_review_auto_variant_then_pre_register"
    )

