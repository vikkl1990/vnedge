import json

from vnedge.research import continuous_research as research
from vnedge.research.universe import ResearchTarget


def test_publish_is_measurement_only_and_cannot_promote(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "OUT_DIR", tmp_path)
    target = ResearchTarget("binanceusdm", "BTC/USDT:USDT", "1h")
    research.publish([{"strategy": "trend_continuation_v1", "verdict": "PASS"}], (target,))
    payload = json.loads((tmp_path / "latest.json").read_text())
    assert payload["policy"] == {
        "measurement_first": True,
        "can_trade": False,
        "can_promote": False,
        "capital_roster_mutation": False,
    }


def test_default_research_filter_is_unrestricted(monkeypatch):
    monkeypatch.delenv("RESEARCH_STRATEGIES", raising=False)
    assert research._enabled_strategies() is None
    monkeypatch.setenv("RESEARCH_STRATEGIES", "trend_continuation_v1,panic_reversal_v1")
    assert research._enabled_strategies() == {
        "trend_continuation_v1",
        "panic_reversal_v1",
    }
