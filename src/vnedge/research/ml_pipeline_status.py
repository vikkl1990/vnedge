"""ML evidence status — bounded, read-only feature/journal audit.

Legacy entry-bar feature reconstruction and research outcomes no longer feed
operational readiness. The status job never trains; final ledger labels and
preregistered chronological validation remain separate delivery requirements.

Run periodically:  python -m vnedge.research.ml_pipeline_status --interval-seconds 300
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from vnedge.ml.drift_supervisor import DRIFT_POLICIES
from vnedge.ml.feature_matrix import (  # noqa: F401
    FEATURE_COLUMNS,
    FeatureParams,
    build_feature_matrix,
)
from vnedge.ml.meta_label_dataset import build_meta_label_dataset, load_lane_journal_trades
from vnedge.ml.lab_audit import build_ml_lab_audit

#: labels needed before role ① can honestly train (>= this, then validate).
MIN_LABELS_TO_TRAIN = 200

#: locked promotion gates (mirror docs/ML_INTEGRATION_PLAN.md — pre-registered).
PROMOTION_GATES = {
    "deflated_sharpe_min": 0.95,
    "pbo_max": 0.20,
    "cpcv_median_profit_factor_min": 1.3,
    "must_beat_rule_based_baseline": True,
    "then": "pre-registered untouched-window judgment -> shadow -> paper -> ladder",
}

_ROLE_ORDER = ("meta_labeling", "regime_permission", "standalone_direction")


def _load_candles(data_root: Path) -> dict:
    """Load normalized candle frames keyed by the ccxt symbol the journals use.

    Covers every venue so binance/bybit (USDT-margined) and Delta (USD) trades
    all resolve. This is the *fallback* source, keyed by symbol only — one
    timeframe per symbol (first frame wins). It cannot align a coarse-timeframe
    entry (a 4h trade) to a fine-timeframe frame, so per-lane caches (below) are
    preferred; the lake covers trades from retired lanes that have no cache.

    ``rglob`` finds the parquet wherever the store nests it (``normalized/`` and
    ``exchange=…`` are both valid roots), so callers can pass either ``data`` or
    ``data/normalized`` without silently loading nothing.
    """
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover
        return {}
    paths: dict = {}
    for path in data_root.rglob("symbol=*/timeframe=*/candles.parquet"):
        parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in path.parts if "=" in p}
        raw = parts.get("symbol", "")
        if raw.endswith("USDT"):
            symbol = f"{raw[:-4]}/USDT:USDT"   # ETHUSDT -> ETH/USDT:USDT
        elif raw.endswith("USD"):
            symbol = f"{raw[:-3]}/USD:USD"      # ETHUSD  -> ETH/USD:USD
        else:
            continue
        paths.setdefault(symbol, path)
    frames = {}
    for symbol, path in paths.items():
        try:
            frames[symbol] = pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            continue
    return frames


def _load_lane_candles(lane_dir: Path) -> dict:
    """Load each lane's own warmup candle cache, keyed by lane id.

    ``<lane_id>.candles.parquet`` holds the exact symbol *and* timeframe the lane
    trades, so a trade tagged with that lane id joins features at the right bar —
    which one-timeframe-per-symbol candles cannot do. Missing/unreadable caches
    are skipped (the symbol lake remains the fallback); never fatal.
    """
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover
        return {}
    frames: dict = {}
    for path in lane_dir.glob("*.candles.parquet"):
        lane_id = path.name[: -len(".candles.parquet")]
        try:
            frames[lane_id] = pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            continue
    return frames


def build_ml_pipeline_status(*, lane_dir: Path, data_root: Path, lab_root: Path | None = None) -> dict:
    """Audit recorded evidence only. Status collection never fits a model.

    data_root remains a CLI compatibility argument; no candle fallback is used.
    The old entry-bar join is exploratory only, not operational label proof.
    """
    audit = build_ml_lab_audit(lane_dir)
    from vnedge.ml.lab_pipeline import pipeline_summary
    pipeline = pipeline_summary(lab_root or Path("research/ml_lab"), lane_dir)
    samples = pipeline.get("ledger_bound_paper_labels", 0)
    try:
        from vnedge.ml.lab_pipeline import read_object
        funding = read_object(data_root.parent / "funding_evidence" / "status.json")
        age = (datetime.now(UTC) - datetime.fromisoformat(funding["generated_at"])).total_seconds()
        funding["stale"] = not 0 <= age < 1900
    except (OSError, ValueError, KeyError, TypeError):
        funding = {"markets": [], "stale": True, "reason": "funding_evidence_unavailable"}
    pipeline["funding_evidence"] = funding
    from vnedge.ml.readiness import readiness_worklist
    pipeline["readiness_worklist"] = readiness_worklist(audit, pipeline)
    validation = None
    trainable = validated = passed = False
    stage = "BLOCKED_LABEL_PROOF" if audit["counts"]["feature_rows"] or audit["counts"]["exit_records"] else "COLLECTING_LABELS"
    if pipeline["runs_total"]:
        stage = "RESEARCH_REPORT_AVAILABLE"

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "active_role": _ROLE_ORDER[0],
        "role_order": list(_ROLE_ORDER),
        "stage": stage,
        "audit_schema": audit["schema"],
        "audit": audit,
        "pipeline": pipeline,
        "training_status": "RESEARCH_REPORT_AVAILABLE" if pipeline["runs_total"] else "BLOCKED_LABEL_PROOF",
        "stages": [
            {"key": "FOUNDATION", "label": "Foundation", "done": True,
             "detail": "validation · robustness · features · dataset builder"},
            {"key": "COLLECTING_LABELS", "label": "Collecting labels", "done": False,
             "detail": f"{samples} / {MIN_LABELS_TO_TRAIN} ledger-bound labels; exit records are not labels", "active": True},
            {"key": "TRAIN", "label": "Train + calibrate", "done": validated,
             "detail": "not run; calibration artifact required",
             "active": trainable and not validated},
            {"key": "VALIDATE", "label": "Validate (DSR/PBO)", "done": passed,
             "detail": "must clear the locked gates", "active": validated and not passed},
            {"key": "SHADOW", "label": "Shadow vs baseline", "done": False,
             "detail": "beat the rule-based baseline OOS", "active": passed},
            {"key": "PAPER", "label": "Paper → ladder", "done": False,
             "detail": "untouched judgment first"},
        ],
        "dataset": {
            "samples": samples,
            "win_rate_pct": None,
            "min_to_train": MIN_LABELS_TO_TRAIN,
            "progress_pct": round(min(100.0, samples / MIN_LABELS_TO_TRAIN * 100), 1),
            "by_strategy": {},
        },
        "foundation": {
            "validation": True,
            "robustness": True,
            "feature_count": len(FEATURE_COLUMNS),
            "dataset_builder": True,
        },
        "online_shadow": {
            "library": "river",
            "installed": importlib.util.find_spec("river") is not None,
            "configured": False,
            "active": False,
            "role": "delayed after-cost probability + alert-only drift monitoring",
            "min_resolved_labels": MIN_LABELS_TO_TRAIN,
            "binding": False,
            "can_trade": False,
            "auto_retrain_live": False,
            "drift_supervisor": {
                "policies_registered": True,
                "configured_streams": len(DRIFT_POLICIES),
                "detectors": sorted(
                    {policy.detector.value for policy in DRIFT_POLICIES.values()}
                ),
                "classes": sorted(
                    {policy.drift_class.value for policy in DRIFT_POLICIES.values()}
                ),
                "event_route": "alert-compatible JSONL + read-only status artifact",
                "automatic_action": "none",
            },
            "note": "requires an explicit pre-registered shadow trial before activation",
        },
        "gates": PROMOTION_GATES,
        # The live gated verdict from the meta-labeling harness (null until there
        # are any labels). Shows the real CPCV PF / DSR / PBO / beats-baseline
        # checks once the dataset clears the CPCV floor.
        "validation": validation,
        "model": None,   # role ① not trained yet — honest null until data + validation
        "can_trade": False,
        "can_promote": False,
        "policy": (
            "batch models trade ONLY via MLStrategy/gateway/registry; River remains "
            "non-binding shadow; judgment on untouched windows only"
        ),
        "note": (
            "Operational labels require a resolved entry/exit/fee/funding ledger "
            "joined to exact feature evidence. Exit submissions and research "
            "outcomes do not count. This worker audits; it never trains."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane-dir", default="logs/paper_trials")
    ap.add_argument("--data-root", default="data/normalized")
    ap.add_argument("--lab-root", default="research/ml_lab")
    ap.add_argument("--output", default="research/live_research/ml_pipeline_status.json")
    ap.add_argument("--interval-seconds", type=float, default=0.0)
    args = ap.parse_args(argv)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    def once() -> None:
        status = build_ml_pipeline_status(
            lane_dir=Path(args.lane_dir), data_root=Path(args.data_root), lab_root=Path(args.lab_root)
        )
        temporary = out.with_suffix(out.suffix + ".tmp")
        temporary.write_text(json.dumps(status, indent=2, allow_nan=False))
        temporary.replace(out)
        print(f"ml_pipeline_status: stage={status['stage']} samples={status['dataset']['samples']}", flush=True)

    once()
    while args.interval_seconds > 0:
        time.sleep(args.interval_seconds)
        try:
            once()
        except Exception as exc:  # noqa: BLE001 - a status job must not crash the fleet
            print(f"ml_pipeline_status error: {exc!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
