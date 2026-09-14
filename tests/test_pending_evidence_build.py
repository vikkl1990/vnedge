from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vnedge.data.delta_lake_repair import coverage_worklist
from vnedge.execution.journal import DecisionJournal
from vnedge.ml.lab_pipeline import pipeline_summary
from vnedge.ml.ledger_labels import iter_records
from vnedge.ml.readiness import readiness_worklist
from vnedge.research.candidate_intake import fork_candidate
from vnedge.research.experiment_packet import ExperimentSpec, digest
from vnedge.strategy.ai_sandbox import load_ai_strategy

ROOT = Path(__file__).resolve().parents[1]


def test_stream_checks_irrelevant_records_and_fixed_prefix(tmp_path):
    path = tmp_path / "lane.journal.jsonl"
    journal = DecisionJournal(path)
    journal.append("evaluation", {"value": 1})
    journal.append("evaluation", {"value": 2})
    reader = iter_records(path, chain="journal")
    assert next(reader)["seq"] == 0
    journal.append("evaluation", {"value": 3})
    assert [r["seq"] for r in reader] == [1]
    rows = list(iter_records(path, chain="journal"))
    rows[-1]["payload"]["value"] = 999
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(ValueError, match="broken_or_legacy_chain"):
        list(iter_records(path, chain="journal"))


def test_stream_size_and_torn_tail_are_not_skipped(tmp_path):
    path = tmp_path / "records"
    path.write_text('{}\n{}')
    with pytest.raises(ValueError, match="incomplete_tail"):
        list(iter_records(path))
    with pytest.raises(ValueError, match="full_history_limit_exceeded"):
        list(iter_records(path, max_bytes=2))
    path.write_text('{"large":12345678}\n')
    with pytest.raises(ValueError, match="record_size_limit_exceeded"):
        list(iter_records(path, max_record_bytes=4))


def test_stream_truncation_during_read_fails(tmp_path):
    path = tmp_path / "records"
    # Larger than the IO buffer, so truncation cannot hide in read-ahead.
    path.write_text('{}\n' + ' ' * 20000 + '\n')
    reader = iter_records(path)
    next(reader)
    path.write_text('{}\n')
    with pytest.raises(ValueError):
        list(reader)


def test_large_journal_is_verified_without_materializing_telemetry(tmp_path):
    from vnedge.ml.ledger_labels import build_ledger_labels
    path = tmp_path / "lane.journal.jsonl"
    journal = DecisionJournal(path)
    journal.append("evaluation", {"value": 1})
    # Whitespace is permitted by the existing JSONL contract. This regression
    # crosses the former 128 MB file ceiling without allocating a 128 MB object.
    with path.open("ab") as stream:
        for _ in range(130):
            stream.write(b" " * 1_000_000 + b"\n")
    fills = tmp_path / "lane.fills.jsonl"
    fills.write_text("")
    result = build_ledger_labels(path, fills)
    assert result["state"] == "VERIFIED"
    assert result["labels"] == []


def test_infrastructure_is_disclosed_not_treated_as_missing_fills(tmp_path):
    (tmp_path / "delta_product_specs.journal.jsonl").write_text('{}\n')
    (tmp_path / "shadow_portfolio.journal.jsonl").write_text('{}\n')
    report = pipeline_summary(tmp_path / "lab", tmp_path)
    assert len(report["non_label_sources"]) == 2
    assert report["ledger_sources"] == []
    assert report["ledger_exclusions"] == {"no_accounting_lanes": 1}
    assert not report["ledger_coverage_complete"]
    (tmp_path / "unknown.fills.jsonl").write_text("")
    report = pipeline_summary(tmp_path / "lab", tmp_path)
    assert report["ledger_exclusions"]["orphan_fill_ledger"] == 1
    assert report["orphan_fill_files"] == ["unknown.fills.jsonl"]


def test_unknown_journal_never_disappears_from_audit(tmp_path):
    (tmp_path / "unknown.journal.jsonl").write_text('{}\n')
    report = pipeline_summary(tmp_path / "lab", tmp_path)
    assert report["ledger_sources"][0]["state"] == "BLOCKED"
    assert not report["ledger_coverage_complete"]


def test_gap_worklist_separates_missing_and_bad_and_caps_ranges():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    times = [start + timedelta(minutes=i) for i in (0, 1, 4, 6)]
    present = set(times) | {start + timedelta(minutes=2)}
    report = coverage_worklist(times, present, 60, limit=1)
    assert report["longest_contiguous_bars"] == 2
    assert report["latest_contiguous_bars"] == 1
    assert report["ranges_truncated"] and report["gap_range_count"] == 2
    assert report["gap_ranges"][0]["absent_slots"] == 1
    assert report["gap_ranges"][0]["stored_unverified_slots"] == 1
    assert report["outside_frame"] == "unknown"
    assert not report["can_repair_from_raw_presence"]


def test_checklist_never_uses_pooled_labels_as_permission():
    items = readiness_worklist({"counts": {"feature_rows": 9999}}, {
        "ledger_bound_paper_labels": 9000, "plans_total": 1,
        "funding_evidence": {"stale": False, "markets": []}})
    keyed = {r["id"]: r for r in items}
    assert keyed["labels"]["status"] == "PENDING"
    assert keyed["funding"]["status"] == "PENDING"
    assert keyed["plan"]["status"] == "RECORDED"
    assert all(not r["can_trade"] and not r["can_promote"] for r in items)


def test_intake_new_identity_only_and_immutable(tmp_path):
    source = ROOT / "data/strategies/ai/example_ma_cross_ai.py"
    raw = source.read_bytes()
    kwargs = {"parent_sha256": digest(raw), "strategy_id": "ai_ma_test_btc_contract_v1",
              "claim": "Closed SMA crossover under the frozen source defaults predicts continuation."}
    first = fork_candidate(source, tmp_path, **kwargs)
    assert fork_candidate(source, tmp_path, **kwargs) == first
    assert source.read_bytes() == raw
    target = tmp_path / "ai_ma_test_btc_contract_v1.py"
    spec = ExperimentSpec.model_validate_json(target.with_suffix(".experiment.json").read_bytes())
    assert spec.source_sha256 == digest(target.read_bytes())
    assert spec.symbol == "BTC/USD:USD" and spec.funding == "excluded"
    assert not first["can_trade"]
    with pytest.raises(ValueError, match="immutable_artifact_conflict"):
        fork_candidate(source, tmp_path, **(kwargs | {"claim": "Another different unproved market claim for a fresh research test."}))
    with pytest.raises(ValueError, match="new_strategy_id_required"):
        fork_candidate(source, tmp_path, **(kwargs | {"strategy_id": "ai_example_ma_cross"}))
    with pytest.raises(ValueError, match="source_hash"):
        fork_candidate(source, tmp_path, **(kwargs | {"parent_sha256": "0" * 64}))


def test_all_reviewed_copies_are_source_identical_except_id():
    catalog = json.loads((ROOT / "research/prereg/arena_intake_20260914.json").read_text())
    directory = ROOT / "data/strategies/ai"
    for row in catalog["candidates"]:
        original = directory / row["file"]
        assert digest(original.read_bytes()) == row["parent_sha256"]
        parent_id = load_ai_strategy(original.read_text()).strategy_id
        target = directory / (parent_id + catalog["new_id_suffix"] + ".py")
        spec = ExperimentSpec.model_validate_json(target.with_suffix(".experiment.json").read_bytes())
        assert spec.strategy_id == load_ai_strategy(target.read_text()).strategy_id
        assert spec.source_sha256 == digest(target.read_bytes())
        trees = [ast.parse(p.read_text()) for p in (original, target)]
        for tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "strategy_id" for t in node.targets):
                    node.value = ast.Constant(value="IDENTITY_ONLY")
        assert ast.dump(trees[0]) == ast.dump(trees[1])


@pytest.mark.parametrize("domain", ["localhost", "127.0.0.1", "a.local", "A.LOCAL", "https://a.com", "a.com:8765", "a.com/path", "*.example.com", "a.com\n{}", "-bad.example.com"])
def test_tls_requires_clean_public_dns_name(domain):
    from vnedge.dashboard.tls_preflight import validate_domain
    with pytest.raises(ValueError):
        validate_domain(domain)


def test_tls_preflight_refuses_mixed_dns_without_contacting_https(monkeypatch):
    from vnedge.dashboard import tls_preflight as tls
    monkeypatch.setattr(tls.socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", ("1.1.1.1", 443)), (0, 0, 0, "", ("2.2.2.2", 443))])
    monkeypatch.setattr(tls, "build_opener", lambda *a: pytest.fail("must not contact mixed DNS"))
    report = tls.preflight("bot.example.com", "1.1.1.1", verify_https=True)
    assert not report["dns_matches"] and not report["https_verified"]


def test_trusted_tls_override_is_opt_in_and_preserves_security():
    import yaml
    override = yaml.safe_load((ROOT / "deploy/compose.trusted-tls.yml").read_text())
    service = override["services"]["dashboard-tls"]
    assert ":?" in service["environment"]["DASHBOARD_DOMAIN"]
    assert all("${DASHBOARD_BIND_IP:-127.0.0.1}" in port for port in service["ports"])
    assert "./deploy/Caddyfile:/etc/caddy/Caddyfile.legacy:ro" in service["volumes"]
    config = (ROOT / "deploy/Caddyfile.trusted").read_text()
    assert "import /etc/caddy/Caddyfile.legacy" in config
    assert "remote_ip {$DASHBOARD_ALLOWLIST:127.0.0.1/32 ::1/128}" in config
    assert 'respond "Forbidden" 403' in config
    assert "tls_insecure_skip_verify" not in config
