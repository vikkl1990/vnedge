

def test_dynamic_atr_trail_ratchets_and_tightens_only():
    from vnedge.runtime.active_exit import ActiveExitState
    from vnedge.strategy.base_strategy import SignalIntent

    st = ActiveExitState.from_signal(
        SignalIntent(side="long", stop_price=98.0, take_profit_price=None,
                     take_profit_levels=(), reason="t"),
        entry_price=100.0, quantity=1.0, trail_atr_mult=2.0,
    )
    # bar runs to 105; no exit; trail arms the stop to 105 - 2*ATR(1) = 103
    assert st.resolve_bar(high=105.0, low=100.0, close=104.0, position_quantity=1.0) is None
    st.trail_stop(atr=1.0)
    assert st.current_stop == 103.0                      # ratcheted up from 98
    # a flat/weaker bar must NOT loosen it
    assert st.resolve_bar(high=104.0, low=103.5, close=104.0, position_quantity=1.0) is None
    st.trail_stop(atr=1.0)                                # mfe still 105 -> stop stays 103
    assert st.current_stop == 103.0
    # price runs higher -> trail follows UP
    st.resolve_bar(high=110.0, low=104.0, close=109.0, position_quantity=1.0)
    st.trail_stop(atr=1.0)
    assert st.current_stop == 108.0                      # 110 - 2
    # a later bar dipping to 108 exits at the trailed stop (locked +8%)
    d = st.resolve_bar(high=109.0, low=107.0, close=108.0, position_quantity=1.0)
    assert d is not None and d.final and d.exit_price == 108.0


def test_trail_off_is_legacy_arm_and_lock():
    from vnedge.runtime.active_exit import ActiveExitState
    from vnedge.strategy.base_strategy import SignalIntent

    st = ActiveExitState.from_signal(
        SignalIntent(side="long", stop_price=98.0, take_profit_price=None,
                     take_profit_levels=(), reason="t"),
        entry_price=100.0, quantity=1.0, trail_atr_mult=0.0,
    )
    st.resolve_bar(high=110.0, low=100.0, close=109.0, position_quantity=1.0)
    st.trail_stop(atr=1.0)
    assert st.current_stop == 98.0  # no trailing when mult=0


def test_exit_engine_same_inputs_produce_identical_decision_sequence():
    from vnedge.runtime.active_exit import ExitEngine, ExitEngineConfig
    from vnedge.strategy.base_strategy import SignalIntent

    signal = SignalIntent(
        side="long",
        stop_price=95.0,
        take_profit_levels=(102.0, 104.0, 106.0),
        reason="parity",
    )
    config = ExitEngineConfig(
        trail_atr_mult=1.5,
        max_holding_bars=4,
        allow_partial_tp=True,
    )
    engines = [
        ExitEngine.from_signal(signal, config=config, entry_price=100.0, quantity=1.0)
        for _surface in ("backtest", "paper", "live-pure")
    ]
    bars = (
        (101.0, 99.0, 100.5, 1.0),
        (102.5, 100.0, 102.0, 1.0),
        (104.5, 102.0, 104.0, 1.0),
    )
    sequences = []
    for engine in engines:
        sequence = []
        quantity = 1.0
        for held, (high, low, close, atr) in enumerate(bars, start=1):
            decision = engine.on_bar(
                high=high,
                low=low,
                close=close,
                position_quantity=quantity,
                atr=atr,
                bars_held=held,
                min_qty=0.01,
                qty_step=0.01,
            )
            if decision is not None:
                sequence.append((decision.reason, decision.quantity, decision.final))
                if not decision.final:
                    quantity -= decision.quantity or 0.0
                    engine.mark_fill(decision)
        sequences.append(sequence)
    assert sequences[0] == sequences[1] == sequences[2]


def test_exit_engine_tick_stop_uses_current_ratcheted_stop():
    from vnedge.runtime.active_exit import ExitEngine, ExitEngineConfig
    from vnedge.strategy.base_strategy import SignalIntent

    engine = ExitEngine.from_signal(
        SignalIntent(side="long", stop_price=95.0, reason="tick"),
        config=ExitEngineConfig(tick_stops_enabled=True),
        entry_price=100.0,
        quantity=1.0,
    )
    engine.state.active_stop_price = 99.0
    assert engine.on_tick(bid=99.5, ask=99.6) is None
    decision = engine.on_tick(bid=98.9, ask=99.0)
    assert decision is not None
    assert decision.reason == "tick_stop"
    assert decision.exit_price == 99.0


def test_exit_engine_live_full_policy_never_emits_partial_quantity():
    from vnedge.runtime.active_exit import ExitEngine, ExitEngineConfig
    from vnedge.strategy.base_strategy import SignalIntent

    engine = ExitEngine.from_signal(
        SignalIntent(
            side="long",
            stop_price=95.0,
            take_profit_levels=(102.0, 104.0, 106.0),
            reason="full-only",
        ),
        config=ExitEngineConfig(allow_partial_tp=False),
        entry_price=100.0,
        quantity=1.0,
    )
    decision = engine.on_bar(
        high=102.5,
        low=100.0,
        close=102.0,
        position_quantity=1.0,
    )
    assert decision is not None
    assert decision.reason == "tp1_partial"
    assert decision.final is True
    assert decision.quantity is None


def test_exit_engine_config_drives_fee_aware_breakeven():
    from vnedge.runtime.active_exit import ExitEngine, ExitEngineConfig
    from vnedge.strategy.base_strategy import SignalIntent

    engine = ExitEngine.from_signal(
        SignalIntent(
            side="long",
            stop_price=95.0,
            take_profit_levels=(102.0, 104.0),
            reason="fee-buffer",
        ),
        config=ExitEngineConfig(fee_aware_breakeven_bps=20.0),
        entry_price=100.0,
        quantity=1.0,
    )
    decision = engine.on_bar(
        high=102.5,
        low=100.0,
        close=102.0,
        position_quantity=1.0,
    )
    assert decision is not None and not decision.final
    engine.mark_fill(decision)
    assert engine.state.current_stop == 100.2


def test_stop_wins_when_stop_and_target_cross_in_same_bar():
    from vnedge.runtime.active_exit import ExitEngine
    from vnedge.strategy.base_strategy import SignalIntent

    engine = ExitEngine.from_signal(
        SignalIntent(
            side="long",
            stop_price=95.0,
            take_profit_levels=(105.0, 110.0),
            reason="tie",
        ),
        entry_price=100.0,
        quantity=1.0,
    )
    decision = engine.on_bar(
        high=106.0,
        low=94.0,
        close=101.0,
        position_quantity=1.0,
    )
    assert decision is not None
    assert decision.reason == "stop"
    assert decision.final is True
    assert decision.exit_price == 95.0


def test_partial_progress_is_acceptance_gated_and_idempotent():
    from vnedge.runtime.active_exit import ExitEngine
    from vnedge.strategy.base_strategy import SignalIntent

    engine = ExitEngine.from_signal(
        SignalIntent(
            side="long",
            stop_price=95.0,
            take_profit_levels=(102.0, 104.0, 106.0),
            reason="acceptance",
        ),
        entry_price=100.0,
        quantity=1.0,
    )
    first = engine.on_bar(
        high=102.5,
        low=100.0,
        close=102.0,
        position_quantity=1.0,
    )
    assert first is not None and first.tp_number == 1

    # No fill acknowledgement: the exact same decision remains pending.
    repeated = engine.on_bar(
        high=102.5,
        low=100.0,
        close=102.0,
        position_quantity=1.0,
    )
    assert repeated is not None and repeated.tp_number == 1
    assert engine.state.tp_index == 0

    engine.mark_fill(first)
    engine.mark_fill(first)  # duplicate reconciliation callback is a no-op
    assert engine.state.tp_index == 1
    assert engine.state.tp_history == [102.0]
    assert engine.state.breakeven_armed is True


def test_restore_preserves_exit_parameters_and_never_loosens_stop():
    from vnedge.runtime.active_exit import ExitEngine, ExitEngineConfig
    from vnedge.strategy.base_strategy import SignalIntent

    signal = SignalIntent(
        side="long",
        stop_price=95.0,
        take_profit_levels=(102.0, 104.0),
        reason="restore",
    )
    source = ExitEngine.from_signal(
        signal,
        config=ExitEngineConfig(
            trail_atr_mult=1.75,
            fee_aware_breakeven_bps=12.0,
        ),
        entry_price=100.0,
        quantity=1.0,
    )
    first = source.on_bar(
        high=102.5,
        low=100.0,
        close=102.0,
        position_quantity=1.0,
    )
    assert first is not None
    source.mark_fill(first)
    stored = source.state.to_dict()
    stored["active_stop_price"] = 90.0  # corrupted: looser than signal stop
    stored["tp_index"] = 99
    stored["tp_history"] = [102.0, float("nan"), -1.0]

    restored = ExitEngine.from_signal(
        signal,
        entry_price=100.0,
        quantity=1.0,
    )
    restored.state.restore(stored)
    assert restored.state.current_stop == 95.0
    assert restored.state.tp_index == 1
    assert restored.state.tp_history == [102.0]
    assert restored.state.breakeven_armed is False
    assert restored.state.trail_atr_mult == 1.75
    assert restored.state.fee_aware_breakeven_bps == 12.0


def test_invalid_exit_inputs_fail_closed():
    import math

    import pytest

    from vnedge.runtime.active_exit import ExitEngine, ExitEngineConfig
    from vnedge.strategy.base_strategy import SignalIntent

    with pytest.raises(ValueError, match="trail_atr_mult"):
        ExitEngineConfig(trail_atr_mult=math.nan)

    engine = ExitEngine.from_signal(
        SignalIntent(side="short", stop_price=105.0, reason="invalid"),
        entry_price=100.0,
        quantity=1.0,
    )
    with pytest.raises(ValueError, match="close must be inside"):
        engine.on_bar(
            high=101.0,
            low=99.0,
            close=102.0,
            position_quantity=1.0,
        )
