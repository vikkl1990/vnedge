import json

import pandas as pd
import pytest

from vnedge.research.runtime_input_capture import capture_runtime_inputs
from vnedge.strategy.htf_regime_continuation_15m_v2_pairs import BTC_STRATEGY_ID


def capture(root, frames):
    return capture_runtime_inputs(root=root, strategy_id=BTC_STRATEGY_ID,
                                  exchange="delta_india", frames=frames)


def test_capture_preserves_unproven_inputs_and_never_grants_admission(tmp_path):
    frame = pd.DataFrame({"timestamp": [pd.Timestamp("2026-09-17", tz="UTC")],
                          "close": [100.], "candle_source": ["exchange_ohlcv"]})
    frames = {tf: frame for tf in ("15m", "4h", "1d")}
    output = capture(tmp_path, frames)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["admission"]["status"] == "INPUTS_REJECTED"
    assert not manifest["can_trade"] and not manifest["can_promote"]
    loaded = pd.read_parquet(output / "15m.parquet")
    pd.testing.assert_frame_equal(loaded.drop(columns="exchange"), frame)
    assert "exchange" not in frame
    assert "content_sha256" not in loaded
    before = (output / "15m.parquet").read_bytes()
    assert capture(tmp_path, frames) != output
    assert (output / "15m.parquet").read_bytes() == before


def test_capture_refuses_conflicting_exchange_and_retention_overflow(tmp_path):
    bad = {tf: pd.DataFrame({"exchange": ["binance"]}) for tf in ("15m", "4h", "1d")}
    with pytest.raises(ValueError, match="identity conflict"):
        capture(tmp_path, bad)
    parent = tmp_path / BTC_STRATEGY_ID
    assert not list(parent.iterdir())
    for i in range(16):
        (parent / str(i)).mkdir()
    with pytest.raises(ValueError, match="retention full"):
        capture(tmp_path, {tf: pd.DataFrame() for tf in bad})
    assert len(list(parent.iterdir())) == 16
