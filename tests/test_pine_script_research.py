"""Pine script research KB tests."""

import json

from vnedge.research.pine_script_research import (
    default_pine_research_payload,
    load_pine_research_payload,
    main,
    publish_pine_research_kb,
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


def test_publish_pine_research_kb_reviews_source_directory(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    pine = source_dir / "open_breakout.pine"
    pine.write_text(
        """
// This Pine Script code is subject to the Mozilla Public License 2.0
//@version=6
strategy("Open Breakout", overlay=true)
atr = ta.atr(14)
long = close > ta.highest(high[1], 20) and volume > ta.sma(volume, 20)
if long
    strategy.entry("L", strategy.long)
    strategy.exit("LX", "L", stop=close - atr, limit=close + atr * 2)
""",
        encoding="utf-8",
    )
    output = tmp_path / "pine_research_kb.json"

    payload = publish_pine_research_kb(
        source_dir=source_dir,
        output_path=output,
        include_defaults=False,
        source_label="unit",
    )

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"] == saved["summary"]
    assert payload["source"] == saved["source"]
    assert saved["summary"]["total"] == 1
    record = saved["records"][0]
    assert record["script_id"] == "open_breakout"
    assert record["title"] == "Open Breakout"
    assert record["source_license"] == "MPL-2.0"
    assert record["source_sha256"]
    assert record["can_trade"] is False
    assert record["can_promote"] is False


def test_pine_research_cli_publishes_artifact(tmp_path, capsys):
    source = tmp_path / "ut_bot.txt"
    source.write_text(
        """
//@version=6
indicator("UT Bot Clone", overlay=true)
trail = ta.ema(close, 10) - ta.atr(14)
plotshape(close > trail)
alertcondition(close > trail, "long")
""",
        encoding="utf-8",
    )
    output = tmp_path / "kb.json"

    assert main([
        str(source),
        "--source-dir",
        str(tmp_path / "missing"),
        "--output",
        str(output),
        "--no-defaults",
    ]) == 0

    printed = capsys.readouterr().out
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert "pine research KB published" in printed
    assert payload["summary"]["total"] == 1
    assert payload["records"][0]["source_available"] is True
