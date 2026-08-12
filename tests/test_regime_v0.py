"""regime_v0 — frozen rules regime model. Filter/sizer only; causal; versioned.

Taxonomy is tested against explicit feature rows (precise, no synthetic-ATR
fighting); causal purity / stability / Registry use real add_regime_columns data.
"""
import pandas as pd

from vnedge.ml.regime_v0 import MODEL_ID, RegimeV0, RegimeV0Params
from vnedge.strategy.regime import RegimeParams


def _row(er, atr_pct, up=False, down=False):
    return pd.Series({"er": er, "atr_pct": atr_pct,
                      "regime_trend_up": up, "regime_trend_down": down})


def _candles(closes, base=1_700_000_000_000, step=3_600_000):
    n = len(closes)
    return pd.DataFrame({
        "timestamp": pd.to_datetime([base + i * step for i in range(n)], unit="ms", utc=True),
        "open": closes, "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes], "close": closes,
        "volume": [10.0] * n,
    })


# --- taxonomy (explicit features) --------------------------------------------
def test_trend_up_allows_long_blocks_short():
    r = RegimeV0().read_row(_row(0.6, 0.5, up=True))
    assert r.label == "trend_up" and r.allow_long and not r.allow_short
    assert r.confidence == 0.6


def test_trend_down_allows_short_blocks_long():
    r = RegimeV0().read_row(_row(0.55, 0.4, down=True))
    assert r.label == "trend_down" and r.allow_short and not r.allow_long


def test_chop_allows_both():
    r = RegimeV0().read_row(_row(0.1, 0.5))
    assert r.label == "chop" and r.allow_long and r.allow_short
    assert abs(r.confidence - 0.9) < 1e-9                    # 1 - er


def test_high_vol_dominates_and_stands_down():
    # high atr_pct overrides even a clean trend and blocks BOTH sides
    r = RegimeV0().read_row(_row(0.6, 0.95, up=True))
    assert r.label == "high_vol" and not r.allow_long and not r.allow_short
    assert r.confidence == 0.95


def test_warmup_is_unknown_neutral_passthrough():
    r = RegimeV0().read_row(_row(float("nan"), float("nan")))
    assert r.label == "unknown"
    assert r.allow_long and r.allow_short                    # never a hidden block
    assert r.confidence == 0.0


def test_high_vol_threshold_is_frozen_at_090():
    m = RegimeV0()
    assert m.read_row(_row(0.6, 0.89, up=True)).label == "trend_up"    # just under
    assert m.read_row(_row(0.6, 0.90, up=True)).label == "high_vol"    # at threshold


# --- schema ------------------------------------------------------------------
def test_output_schema_and_model_id():
    d = RegimeV0().read_row(_row(0.6, 0.5, up=True)).to_dict()
    assert set(d) == {"label", "allow_long", "allow_short", "confidence",
                      "scores", "features_used", "model_id"}
    assert d["model_id"] == MODEL_ID
    assert set(d["scores"]) == {"trend_up", "trend_down", "chop", "high_vol"}
    assert list(d["features_used"]) == ["er", "atr_pct", "regime_trend_up", "regime_trend_down"]


# --- causal purity (real features) -------------------------------------------
def test_causal_purity_future_bars_do_not_change_the_reading():
    closes = [100.0 + i * 0.4 for i in range(300)] + [220.0 + (2 if i % 2 else -2) for i in range(100)]
    full = _candles(closes)
    m = RegimeV0()
    k = 320
    r_full = m.classify(full, k)
    r_trunc = m.classify(full.iloc[: k + 1].reset_index(drop=True), k)
    assert r_full.to_dict() == r_trunc.to_dict()            # no lookahead


# --- stability (no 1-bar flip-flop on a stable series) -----------------------
def test_stability_no_single_bar_flips_on_clean_series():
    m = RegimeV0()
    df = m.prepare(_candles([100.0 + i * 0.4 for i in range(400)]))
    labels = [m.read_row(df.iloc[i]).label for i in range(m.warmup_bars, 400)]
    flips = sum(1 for a, b in zip(labels, labels[1:]) if a != b)
    assert flips <= 3                                       # one dominant state, no thrash


# --- Registry round-trip through the real ladder -----------------------------
def test_registry_register_promote_and_load(tmp_path):
    from vnedge.ml.model_registry import ModelRegistry
    reg = ModelRegistry(root=tmp_path / "models")
    m = RegimeV0()
    reg.register(m.metadata(), m.artifact_bytes())
    assert reg.get(MODEL_ID).status == "research"
    reg.update_status(MODEL_ID, "candidate", "prereg locked")
    reg.update_status(MODEL_ID, "paper", "shadow overlay")
    loaded = reg.load_artifact(MODEL_ID)                     # only loadable at paper/promoted
    assert loaded["model_id"] == MODEL_ID
    assert loaded["params"]["high_vol_pct"] == 0.90


def test_config_hash_is_stable_and_param_sensitive():
    assert RegimeV0().config_hash() == RegimeV0().config_hash()
    other = RegimeV0(RegimeV0Params(regime=RegimeParams(), high_vol_pct=0.85))
    assert RegimeV0().config_hash() != other.config_hash()
