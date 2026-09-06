from vnedge.runtime.readiness import build_runtime_readiness


def test_readiness_is_monotonic_and_deduplicates_blockers() -> None:
    result = build_runtime_readiness(
        data_blockers=(None, "candle_gap", "candle_gap"),
        decision_blockers=("warmup",),
        parity_blockers=("approval_parity",),
        execution_blockers=("private_stream_unhealthy",),
        live_blockers=("capital_locked",),
    )

    assert result.data_ready is False
    assert result.decision_ready is False
    assert result.execution_ready is False
    assert result.parity_ready is False
    assert result.live_ready is False
    assert result.data_blockers == ("candle_gap",)
    assert result.decision_blockers == ("candle_gap", "warmup")
    assert result.execution_blockers == (
        "candle_gap",
        "warmup",
        "private_stream_unhealthy",
    )
    assert result.parity_blockers == ("candle_gap", "warmup", "approval_parity")
    assert "capital_locked" in result.live_blockers


def test_readiness_is_true_only_when_every_layer_is_clear() -> None:
    result = build_runtime_readiness()

    assert result.data_ready
    assert result.decision_ready
    assert result.execution_ready
    assert result.parity_ready
    assert result.live_ready
    assert result.to_dict()["execution_blockers"] == ()
