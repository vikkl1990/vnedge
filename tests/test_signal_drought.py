from datetime import UTC, datetime, timedelta

import pytest

from vnedge.runtime.signal_drought import SignalDroughtTracker

NOW = datetime(2026, 9, 5, 12, 15, tzinfo=UTC)


def tracker() -> SignalDroughtTracker:
    return SignalDroughtTracker(
        lane_id="delta_btc_htf",
        strategy_id="htf_regime_continuation_15m_v3",
        symbol="BTC/USD:USD",
        timeframe="15m",
        path_id="kernel_v1",
    )


def note_eval(
    value: SignalDroughtTracker,
    *,
    at: datetime = NOW,
    eligible: bool = False,
    fired: bool = False,
    decision_id: str | None = None,
    primary: str | None = "playbook_blocked",
    all_failed: tuple[str, ...] = ("playbook_blocked",),
    quotes_armed: bool | None = None,
) -> None:
    value.note_eval(
        decision_open=at - timedelta(minutes=15),
        decision_close=at,
        evaluated_at=at + timedelta(seconds=2),
        eligible=eligible,
        fired=fired,
        decision_id=decision_id,
        primary_failed_gate=primary,
        all_failed_gates=all_failed,
        skip_runtime=None,
        candle_source="canonical_tick_lake",
        decision_transport="router",
        mreg_ready=True,
        structure_ready=None,
        quotes_armed=quotes_armed,
    )


def test_event_clocks_source_transport_and_tri_state_are_distinct() -> None:
    value = tracker()
    note_eval(value, eligible=False)

    out = value.snapshot(now=NOW + timedelta(seconds=10), timeframe_seconds=900)

    assert out.last_decision_open == (NOW - timedelta(minutes=15)).isoformat()
    assert out.last_decision_close == NOW.isoformat()
    assert out.last_eval_at == (NOW + timedelta(seconds=2)).isoformat()
    assert out.eval_age_s == 8.0
    assert out.candle_source == "canonical_tick_lake"
    assert out.decision_transport == "router"
    assert out.mreg_ready is True
    assert out.structure_ready is None
    assert out.quotes_armed is None
    assert out.drought_class == "playbook_wait"


def test_evidence_resets_only_evidence_age_and_keeps_rolling_histograms() -> None:
    value = tracker()
    note_eval(
        value,
        primary="market_regime_playbook_blocked",
        all_failed=("market_regime_playbook_blocked", "no_reclaim"),
    )
    value.note_evidence(decision_id="dec_abc", persisted_at=NOW + timedelta(seconds=3))

    out = value.snapshot(now=NOW + timedelta(seconds=13), timeframe_seconds=900)

    assert out.evidence_age_s == 10.0
    assert out.last_decision_id == "dec_abc"
    assert out.primary_gate_counts_24h == {"market_regime_playbook_blocked": 1}
    assert out.all_failed_gate_counts_24h == {
        "market_regime_playbook_blocked": 1,
        "no_reclaim": 1,
    }


def test_setup_without_accept_is_quote_or_cost_wait() -> None:
    value = tracker()
    note_eval(
        value,
        eligible=True,
        decision_id="dec_setup",
        primary=None,
        all_failed=(),
        quotes_armed=True,
    )
    value.note_evidence(decision_id="dec_setup", persisted_at=NOW + timedelta(seconds=3))

    out = value.snapshot(now=NOW + timedelta(seconds=10), timeframe_seconds=900)

    assert out.setup_age_s == 10.0
    assert out.accept_age_s is None
    assert out.drought_class == "quote_or_cost_wait"


def test_fire_without_envelope_is_identity_bug() -> None:
    value = tracker()
    note_eval(value, eligible=True, fired=True, decision_id=None, primary=None, all_failed=())

    assert (
        value.snapshot(now=NOW + timedelta(seconds=10), timeframe_seconds=900).drought_class
        == "identity_bug"
    )


def test_long_eval_age_is_ops_silent() -> None:
    value = tracker()
    note_eval(value, decision_id="dec_old")
    value.note_evidence(decision_id="dec_old", persisted_at=NOW + timedelta(seconds=3))

    out = value.snapshot(now=NOW + timedelta(minutes=24), timeframe_seconds=900)

    assert out.drought_class == "ops_silent"


def test_primary_and_all_histograms_roll_for_24_hours() -> None:
    value = tracker()
    note_eval(value, at=NOW - timedelta(hours=25), primary="old", all_failed=("old",))
    note_eval(value, at=NOW, primary="new", all_failed=("new", "secondary"))

    out = value.snapshot(now=NOW + timedelta(seconds=5), timeframe_seconds=900)

    assert out.primary_gate_counts_24h == {"new": 1}
    assert out.all_failed_gate_counts_24h == {"new": 1, "secondary": 1}


def test_naive_event_clock_is_rejected() -> None:
    value = tracker()
    with pytest.raises(ValueError, match="timezone-aware"):
        value.note_evidence(decision_id="dec", persisted_at=NOW.replace(tzinfo=None))


def test_restore_preserves_rolling_counts_and_envelope_clocks() -> None:
    value = tracker()
    persisted = NOW + timedelta(seconds=4)
    value.restore(
        [
            {
                "ts": persisted.isoformat(),
                "kind": "lane_eval",
                "payload": {
                    "strategy_id": value.strategy_id,
                    "symbol": value.symbol,
                    "timeframe": value.timeframe,
                    "bar_ts": (NOW - timedelta(minutes=15)).isoformat(),
                    "decision_at": NOW.isoformat(),
                    "eval_at": (NOW + timedelta(seconds=2)).isoformat(),
                    "eligible": True,
                    "fired": True,
                    "decision_ids": ["dec_restored"],
                    "primary_failed_gate": "fee_floor",
                    "all_failed_gates": ["fee_floor", "min_net"],
                    "mreg_ready": True,
                    "structure_ready": None,
                    "quotes_armed": False,
                    "data_source": {
                        "candle_source": "canonical_tick_lake",
                        "decision_transport": "parquet",
                    },
                },
            },
            {
                "ts": (persisted + timedelta(seconds=1)).isoformat(),
                "kind": "candidate_evaluation",
                "payload": {
                    "strategy_id": value.strategy_id,
                    "symbol": value.symbol,
                    "approved": True,
                    "execution_evidence": {"decision_id": "dec_restored"},
                },
            },
        ]
    )

    out = value.snapshot(now=NOW + timedelta(seconds=10), timeframe_seconds=900)

    assert out.last_evidence_at == persisted.isoformat()
    assert out.accept_age_s == 5.0
    assert out.primary_gate_counts_24h == {"fee_floor": 1}
    assert out.all_failed_gate_counts_24h == {"fee_floor": 1, "min_net": 1}
    assert out.drought_class == "healthy_wait"
