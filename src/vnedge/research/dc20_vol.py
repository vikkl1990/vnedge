"""Frozen DC20 volatility comparisons; no live registry or execution authority."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from vnedge.research import st4h_dc20


def entries(result: dict) -> dict[str, dict]:
    return {f'{e["entry_ts"]}/{e["side"]}': e for e in result["events"] if e["action"] == "SIGNAL"}


def compare(baseline: dict, variant: dict) -> dict:
    original, new = entries(baseline), entries(variant)
    removed, added = sorted(original.keys()-new.keys()), sorted(new.keys()-original.keys())
    return {"same_book": all(baseline[k] == variant[k] for k in ("trades", "open_position", "unresolved")),
            "baseline_entries": len(original), "variant_entries": len(new),
            "shared_entry_ids": sorted(original.keys() & new.keys()),
            "removed_entry_ids": removed, "added_entry_ids": added}


def run(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    source = Path("research/st4h_dc20_v1/dc20_attempt_01/bars.parquet")
    contract = Path("research/dc20_vol_v1/CONTRACT.md")
    frame = pd.read_parquet(source)
    end = pd.Timestamp("2026-09-20", tz="UTC")
    report = {"source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
              "code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (Path(__file__), Path(st4h_dc20.__file__))},
              "can_trade": False, "can_promote": False, "funding_included": False, "results": []}
    (output/"CONTRACT.md").write_bytes(contract.read_bytes())
    for window, start in (("full", pd.Timestamp("2025-01-01", tz="UTC")),
                          ("90d", end-pd.Timedelta(days=90)), ("14d", end-pd.Timedelta(days=14))):
        baseline = st4h_dc20.replay(frame, "DC20", start, end)
        old = json.loads(Path(f"research/st4h_dc20_v1/dc20_attempt_01/wilder_{window}.json").read_text())
        for field in ("metrics", "trades", "open_position", "unresolved"):
            if baseline[field] != old[field]:
                raise ValueError(f"baseline changed: {window}/{field}")
        for mode in st4h_dc20.VOL_MODES:
            result = baseline if mode == "off" else st4h_dc20.replay(frame, "DC20", start, end, vol_mode=mode)
            result.update(window=window, vol_mode=mode, comparison=compare(baseline, result))
            result["metrics"]["net_excluding_largest_winner"] = (
                result["metrics"]["net"]-max([0.]+[t["net"] for t in result["trades"]]))
            ratios = [{k: e[k] for k in ("entry_ts", "side", "atr_ratio", "adr_ratio")}
                      for e in entries(result).values()]
            result["entry_ratios"] = ratios
            (output/f"{window}_{mode}.json").write_text(json.dumps(result, indent=2, allow_nan=False))
            report["results"].append({k: v for k, v in result.items() if k not in ("events", "trades")})
            print(window, mode, result["metrics"], result["comparison"], flush=True)
    (output/"report.json").write_text(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
