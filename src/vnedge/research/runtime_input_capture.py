"""Bounded immutable startup input snapshots; never repair or authorize data."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd

from vnedge.research.paper_path_replay import preflight_bundle, source_policy_contract


def capture_runtime_inputs(*, root: Path, strategy_id: str, exchange: str,
                           frames: dict[str, pd.DataFrame]) -> Path:
    """Capture inputs supplied to prepare/context bind, including rejected rows.

    Exchange identity comes from the runtime lane, not inferred OHLC values.
    No source, coverage or content hash is generated. At most 16 snapshots per
    registration are retained; a full store requires explicit operator archival.
    A manifest is the completion marker; interrupted captures remain inspectable.
    """
    policy = source_policy_contract(strategy_id, "registered_context_v1")
    if exchange != "delta_india" or set(frames) != {"15m", "4h", "1d"}:
        raise ValueError("capture requires the registered Delta input set")
    parent = root / strategy_id
    parent.mkdir(parents=True, exist_ok=True)
    if len(list(parent.iterdir())) >= 16:
        raise ValueError("input capture retention full; archive explicitly")
    copies = {tf: frame.copy(deep=True) for tf, frame in frames.items()}
    for frame in copies.values():
        if "exchange" in frame and not frame.exchange.eq(exchange).all():
            raise ValueError("capture exchange identity conflict")
    output = parent / uuid4().hex
    output.mkdir(exist_ok=False)
    paths: dict[str, Path] = {}
    for tf, frame in copies.items():
        if "exchange" not in frame:
            frame["exchange"] = exchange
        paths[tf] = output / f"{tf}.parquet"
        frame.to_parquet(paths[tf], index=False)
    admission = preflight_bundle(strategy_id=strategy_id, candles=paths["15m"],
        h4=paths["4h"], daily=paths["1d"], source_policy="registered_context_v1")
    manifest = {
        "schema_version": 1, "scope": "runtime_startup_inputs_not_historical_asof_proof",
        "captured_at": datetime.now(UTC).isoformat(), "source_policy": policy,
        "exchange_identity_authority": "runtime_lane_spec",
        "transform": "exchange column added only if absent; all other columns preserved",
        "files": {tf: {"name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for tf, path in paths.items()},
        "admission": admission, "can_trade": False, "can_promote": False,
    }
    with (output / "manifest.json").open("x") as handle:
        json.dump(manifest, handle, indent=2, default=str)
    return output
