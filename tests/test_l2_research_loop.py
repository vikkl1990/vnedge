"""Decoupled L2 research loop — run_once composition, atomic publish, merge."""

import json

from vnedge.research import continuous_research as cr
from vnedge.research import l2_research_loop as l2


def test_run_once_composes_scalper_and_alpha(monkeypatch, tmp_path):
    targets = (cr.ResearchTarget("binanceusdm", "BTC/USDT:USDT"),)
    monkeypatch.setattr(l2, "load_research_targets", lambda: targets)
    monkeypatch.setattr(l2, "_scalper_research_days", lambda root, t: ("20260705",))
    monkeypatch.setattr(l2, "run_scalper_research",
                        lambda root, t, days: {"flow_guards": {"can_trade": False},
                                               "edge_hypotheses": [1, 2]})
    monkeypatch.setattr(l2, "run_alpha_factory",
                        lambda root, t, days, max_rows: {"flow_guards": {"can_trade": False},
                                                         "hypotheses": [1]})
    payload = l2.run_once(tmp_path)
    assert payload["days"] == ["20260705"]
    assert payload["scalper_research"]["edge_hypotheses"] == [1, 2]
    assert payload["alpha_factory"]["hypotheses"] == [1]
    assert "generated_at" in payload


def test_publish_l2_is_atomic(tmp_path):
    l2.publish_l2({"scalper_research": {"x": 1}}, out_dir=tmp_path)
    assert not list(tmp_path.glob("*.tmp"))                 # no leftover temp
    got = json.loads((tmp_path / "l2_latest.json").read_text())
    assert got["scalper_research"] == {"x": 1}


def test_candle_loop_folds_in_l2_latest_when_inline_disabled(tmp_path, monkeypatch):
    # the candle loop reuses the decoupled loop's last L2 output
    monkeypatch.setattr(cr, "OUT_DIR", tmp_path)
    (tmp_path / "l2_latest.json").write_text(json.dumps({
        "scalper_research": {"flow_guards": {"can_trade": False}, "edge_hypotheses": [1]},
        "alpha_factory": {"flow_guards": {"can_trade": False}},
    }))
    l2latest = cr._load_l2_latest()
    assert l2latest["scalper_research"]["edge_hypotheses"] == [1]
    # emulate the run_cycle merge: inline empty -> fold in decoupled output
    scalper_research, alpha_factory = {}, {}
    scalper_research = scalper_research or l2latest.get("scalper_research", {})
    alpha_factory = alpha_factory or l2latest.get("alpha_factory", {})
    assert scalper_research["edge_hypotheses"] == [1]
    assert alpha_factory["flow_guards"]["can_trade"] is False


def test_load_l2_latest_missing_or_corrupt_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "OUT_DIR", tmp_path)
    assert cr._load_l2_latest() == {}                       # missing
    (tmp_path / "l2_latest.json").write_text("{ not json")
    assert cr._load_l2_latest() == {}                       # corrupt -> {} not a crash
