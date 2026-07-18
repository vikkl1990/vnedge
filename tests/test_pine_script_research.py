"""Pine script research KB tests."""

import json

from vnedge.research.pine_script_research import (
    default_pine_research_payload,
    load_pine_research_payload,
    review_pine_source,
)


def test_default_pine_research_payload_is_research_only():
    payload = default_pine_research_payload()

    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["summary"]["total"] >= 3
    assert payload["summary"]["portable"] >= 1
    assert any(r["script_id"] == "luxara_live_plan_qtm_v1" for r in payload["records"])
    assert any(r["script_id"] == "luxara_break_bounce_v27_v1" for r in payload["records"])


def test_load_pine_research_payload_summarizes_generated_artifact(tmp_path):
    path = tmp_path / "pine_research_kb.json"
    path.write_text(json.dumps({
        "generated_at": "2026-07-18T00:00:00+00:00",
        "source": "unit",
        "records": [
            {
                "script_id": "open_ut_bot",
                "title": "Open UT Bot",
                "url": "https://www.tradingview.com/script/example/",
                "crypto_portability": "PORTABLE_WITH_CHANGES",
                "crypto_fit_score": 76,
                "backtests": [{"timeframe": "5m", "status": "queued"}],
            },
            {
                "script_id": "protected_overlay",
                "title": "Protected Overlay",
                "url": "https://www.tradingview.com/script/locked/",
                "crypto_portability": "BLOCKED_NO_SOURCE",
                "crypto_fit_score": 5,
                "backtests": [],
            },
        ],
    }))

    payload = load_pine_research_payload(path)

    assert payload["summary"] == {
        "total": 2,
        "portable": 1,
        "needs_source": 1,
        "research_only": 0,
        "blocked_repaint": 0,
        "backtests_queued": 1,
    }
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_review_pine_source_flags_repaint_risk():
    source = """
//@version=6
indicator("MTF risky", overlay=true)
htf = request.security(syminfo.tickerid, "60", close, lookahead=barmerge.lookahead_on)
plot(htf)
"""

    record = review_pine_source(
        script_id="mtf_risky",
        title="MTF Risky",
        url="https://www.tradingview.com/script/risky/",
        source=source,
        source_license="MPL-2.0",
    )

    assert record.source_available is True
    assert record.source_sha256
    assert record.crypto_portability == "BLOCKED_REPAINT_RISK"
    assert "lookahead_on" in record.risks
    assert all(cell.status == "blocked" for cell in record.backtests)


def test_review_pine_source_scores_portable_crypto_mechanics():
    source = """
//@version=6
strategy("Breakout", overlay=true)
ema50 = ta.ema(close, 50)
atr14 = ta.atr(14)
volOk = volume > ta.sma(volume, 20) * 2
long = close > ta.highest(high[1], 12) and close > ema50 and volOk
if long
    strategy.entry("L", strategy.long)
    strategy.exit("LX", "L", stop=close - atr14, limit=close + atr14 * 2)
"""

    record = review_pine_source(
        script_id="breakout",
        title="Breakout",
        url="user_supplied",
        source=source,
    )

    assert record.kind == "strategy"
    assert record.crypto_fit_score >= 45
    assert record.crypto_portability in {"PORTABLE", "PORTABLE_WITH_CHANGES"}
    assert "breakout" in record.features
    assert "risk_plan" in record.features
