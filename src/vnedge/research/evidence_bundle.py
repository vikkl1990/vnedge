"""Immutable, content-addressed evidence bundles for VNEDGE research.

One completed canonical backtest report becomes one bundle.  Bundles make the
experiment contract (code, data, parameters, engine, costs and parity status)
auditable without granting any trading or promotion authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vnedge.backtest.report import REPORT_SCHEMA

BUNDLE_SCHEMA = "vnedge.research_evidence_bundle.v2"
INDEX_SCHEMA = "vnedge.research_evidence_index.v2"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True
    )


class ArtifactDigest(_FrozenModel):
    path: str
    sha256: str | None = None
    bytes: int | None = Field(default=None, ge=0)


class EngineContract(_FrozenModel):
    name: str
    version: str
    parity_status: Literal["EXACT", "MISMATCH", "NOT_REPORTED"] = "NOT_REPORTED"
    parity_artifact: ArtifactDigest | None = None


class BundleGovernance(_FrozenModel):
    can_trade: Literal[False] = False
    can_promote: Literal[False] = False
    live_orders_enabled: Literal[False] = False
    read_only: Literal[True] = True


class EvidenceBundleManifest(_FrozenModel):
    schema_id: Literal["vnedge.research_evidence_bundle.v2"] = Field(
        default="vnedge.research_evidence_bundle.v2", alias="schema"
    )
    bundle_id: str
    manifest_sha256: str
    generated_at: str
    run_id: str
    strategy_id: str
    revision_id: str
    evidence_class: str
    exchange: str
    symbol: str
    timeframes: tuple[str, ...]
    window_start: str | None = None
    window_end: str | None = None
    code_sha: str
    data: ArtifactDigest
    parameters_sha256: str
    parameters: dict[str, Any]
    engine: EngineContract
    cost_contract: dict[str, Any]
    cost_contract_sha256: str
    report: ArtifactDigest
    metrics: dict[str, Any]
    warnings: tuple[str, ...]
    governance: BundleGovernance = BundleGovernance()


class BundleVerification(_FrozenModel):
    bundle_id: str
    valid: bool
    errors: tuple[str, ...] = ()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _artifact(path_value: str, *, supplied_sha256: str | None = None) -> ArtifactDigest:
    path = Path(path_value)
    if supplied_sha256:
        return ArtifactDigest(path=path_value, sha256=supplied_sha256)
    if path.is_file():
        sha256, size = _file_digest(path)
        return ArtifactDigest(path=path_value, sha256=sha256, bytes=size)
    return ArtifactDigest(path=path_value)


def _safe_metrics(report: dict[str, Any]) -> dict[str, Any]:
    overview = report.get("overview")
    source = overview if isinstance(overview, dict) else {}
    keys = (
        "num_trades",
        "gross_profit_usd",
        "net_profit_usd",
        "total_cost_usd",
        "return_pct",
        "profit_factor",
        "sharpe",
        "max_drawdown_pct",
        "win_rate_pct",
    )
    return {key: source.get(key) for key in keys}


def _manifest_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in manifest.items()
        if key not in {"bundle_id", "manifest_sha256"}
    }


def publish_backtest_bundle(
    report: dict[str, Any],
    *,
    root: Path | str,
    index_path: Path | str | None = None,
    code_sha: str = "UNKNOWN",
    revision_id: str | None = None,
    data_sha256: str | None = None,
    engine_version: str = "UNKNOWN",
    parity_status: Literal["EXACT", "MISMATCH", "NOT_REPORTED"] = "NOT_REPORTED",
    parity_artifact: Path | str | None = None,
) -> EvidenceBundleManifest:
    """Publish one canonical report atomically and index it idempotently."""
    if report.get("schema") != REPORT_SCHEMA:
        raise ValueError(f"expected {REPORT_SCHEMA}, got {report.get('schema')!r}")
    governance = report.get("governance")
    if not isinstance(governance, dict) or governance.get("can_trade") is not False:
        raise ValueError("canonical report must explicitly deny trading authority")
    if governance.get("can_promote") is not False:
        raise ValueError("canonical report must explicitly deny promotion authority")

    run_value = report.get("run")
    if not isinstance(run_value, dict):
        raise TypeError("canonical report is missing run contract")
    run = run_value
    run_id = str(run.get("run_id") or "").strip()
    strategy_id = str(run.get("strategy_id") or "").strip()
    if not run_id or not strategy_id:
        raise ValueError("run_id and strategy_id are required")

    report_bytes = _canonical(report)
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    data_source = str(run.get("data_source") or "not_reported")
    parameters_value = run.get("parameters")
    parameters: dict[str, Any] = (
        parameters_value if isinstance(parameters_value, dict) else {}
    )
    costs_value = run.get("costs")
    costs: dict[str, Any] = costs_value if isinstance(costs_value, dict) else {}
    window_value = run.get("window")
    window: dict[str, Any] = window_value if isinstance(window_value, dict) else {}
    parity_digest = (
        _artifact(str(parity_artifact)) if parity_artifact is not None else None
    )
    draft: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        # Use the canonical report timestamp so republishing identical evidence
        # is idempotent instead of manufacturing a second content identity.
        "generated_at": str(run.get("generated_at") or datetime.now(UTC).isoformat()),
        "run_id": run_id,
        "strategy_id": strategy_id,
        "revision_id": revision_id or f"{strategy_id}@unlinked",
        "evidence_class": str(run.get("evidence_class") or "EXPLORATORY"),
        "exchange": str(run.get("exchange") or "unknown"),
        "symbol": str(run.get("symbol") or "unknown"),
        "timeframes": (str(run.get("timeframe") or "unknown"),),
        "window_start": window.get("start"),
        "window_end": window.get("end"),
        "code_sha": code_sha or "UNKNOWN",
        "data": _artifact(data_source, supplied_sha256=data_sha256).model_dump(mode="json"),
        "parameters_sha256": _sha(parameters),
        "parameters": parameters,
        "engine": EngineContract(
            name=str(run.get("engine") or "unknown"),
            version=engine_version or "UNKNOWN",
            parity_status=parity_status,
            parity_artifact=parity_digest,
        ).model_dump(mode="json"),
        "cost_contract": costs,
        "cost_contract_sha256": _sha(costs),
        "report": ArtifactDigest(
            path="report.json", sha256=report_sha256, bytes=len(report_bytes)
        ).model_dump(mode="json"),
        "metrics": _safe_metrics(report),
        "warnings": tuple(str(item) for item in report.get("warnings", ())),
        "governance": BundleGovernance().model_dump(mode="json"),
    }
    manifest_sha256 = _sha(draft)
    draft["bundle_id"] = f"veb_{manifest_sha256}"
    draft["manifest_sha256"] = manifest_sha256
    manifest = EvidenceBundleManifest.model_validate(draft)

    root_path = Path(root)
    destination = root_path / manifest.bundle_id
    root_path.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        verification = verify_bundle(destination)
        if not verification.valid:
            raise ValueError(
                f"existing bundle {manifest.bundle_id} failed verification: "
                + "; ".join(verification.errors)
            )
    else:
        staging = root_path / f".{manifest.bundle_id}.{uuid.uuid4().hex}.tmp"
        try:
            staging.mkdir(parents=False)
            (staging / "report.json").write_bytes(report_bytes)
            (staging / "manifest.json").write_text(
                json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            try:
                os.replace(staging, destination)
            except OSError:
                # Another worker may have published the identical
                # content-addressed directory after our existence check.
                if not destination.exists() or not verify_bundle(destination).valid:
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    resolved_index = Path(index_path) if index_path is not None else root_path / "index.sqlite"
    index_bundle(manifest, destination, resolved_index)
    return manifest


def verify_bundle(bundle_path: Path | str) -> BundleVerification:
    path = Path(bundle_path)
    errors: list[str] = []
    manifest_path = path / "manifest.json"
    report_path = path / "report.json"
    bundle_id = path.name
    try:
        manifest = EvidenceBundleManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        bundle_id = manifest.bundle_id
    except (OSError, ValueError) as exc:
        return BundleVerification(bundle_id=bundle_id, valid=False, errors=(str(exc),))
    if path.name != manifest.bundle_id:
        errors.append("directory name does not match bundle_id")
    expected_manifest_sha = _sha(_manifest_payload(manifest.model_dump(mode="json")))
    if expected_manifest_sha != manifest.manifest_sha256:
        errors.append("manifest digest mismatch")
    if manifest.bundle_id != f"veb_{expected_manifest_sha}":
        errors.append("content-addressed bundle_id mismatch")
    try:
        report_sha, report_size = _file_digest(report_path)
        if report_sha != manifest.report.sha256:
            errors.append("report digest mismatch")
        if report_size != manifest.report.bytes:
            errors.append("report size mismatch")
    except OSError as exc:
        errors.append(str(exc))
    return BundleVerification(bundle_id=bundle_id, valid=not errors, errors=tuple(errors))


def load_bundle(
    bundle_path: Path | str, *, verify: bool = True
) -> tuple[EvidenceBundleManifest, dict[str, Any]]:
    path = Path(bundle_path)
    if verify:
        result = verify_bundle(path)
        if not result.valid:
            raise ValueError("; ".join(result.errors))
    manifest = EvidenceBundleManifest.model_validate_json(
        (path / "manifest.json").read_text(encoding="utf-8")
    )
    report = json.loads((path / "report.json").read_text(encoding="utf-8"))
    return manifest, report


def _connect(index_path: Path) -> sqlite3.Connection:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(index_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS evidence_bundles (
          bundle_id TEXT PRIMARY KEY,
          schema TEXT NOT NULL,
          run_id TEXT NOT NULL,
          strategy_id TEXT NOT NULL,
          revision_id TEXT NOT NULL,
          evidence_class TEXT NOT NULL,
          exchange TEXT NOT NULL,
          symbol TEXT NOT NULL,
          timeframe TEXT NOT NULL,
          window_start TEXT,
          window_end TEXT,
          generated_at TEXT NOT NULL,
          code_sha TEXT NOT NULL,
          data_sha256 TEXT,
          parameters_sha256 TEXT NOT NULL,
          cost_contract_sha256 TEXT NOT NULL,
          report_sha256 TEXT NOT NULL,
          engine TEXT NOT NULL,
          engine_version TEXT NOT NULL,
          parity_status TEXT NOT NULL,
          num_trades INTEGER,
          gross_profit_usd REAL,
          net_profit_usd REAL,
          total_cost_usd REAL,
          profit_factor REAL,
          max_drawdown_pct REAL,
          bundle_path TEXT NOT NULL,
          manifest_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_strategy
          ON evidence_bundles(strategy_id, generated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_evidence_market
          ON evidence_bundles(exchange, symbol, timeframe, generated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence_bundles(run_id);
        CREATE TABLE IF NOT EXISTS evidence_index_metadata (
          schema TEXT PRIMARY KEY,
          created_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO evidence_index_metadata VALUES (?, ?)",
        (INDEX_SCHEMA, datetime.now(UTC).isoformat()),
    )
    connection.commit()
    return connection


def index_bundle(
    manifest: EvidenceBundleManifest,
    bundle_path: Path | str,
    index_path: Path | str,
) -> None:
    metrics = manifest.metrics
    values = (
        manifest.bundle_id,
        BUNDLE_SCHEMA,
        manifest.run_id,
        manifest.strategy_id,
        manifest.revision_id,
        manifest.evidence_class,
        manifest.exchange,
        manifest.symbol,
        manifest.timeframes[0],
        manifest.window_start,
        manifest.window_end,
        manifest.generated_at,
        manifest.code_sha,
        manifest.data.sha256,
        manifest.parameters_sha256,
        manifest.cost_contract_sha256,
        manifest.report.sha256,
        manifest.engine.name,
        manifest.engine.version,
        manifest.engine.parity_status,
        metrics.get("num_trades"),
        metrics.get("gross_profit_usd"),
        metrics.get("net_profit_usd"),
        metrics.get("total_cost_usd"),
        metrics.get("profit_factor"),
        metrics.get("max_drawdown_pct"),
        str(Path(bundle_path)),
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
    )
    connection = _connect(Path(index_path))
    try:
        with connection:
            connection.execute(
                """INSERT OR IGNORE INTO evidence_bundles VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
    finally:
        connection.close()


def list_bundle_index(index_path: Path | str, *, limit: int = 500) -> list[dict[str, Any]]:
    path = Path(index_path)
    if not path.exists():
        return []
    # Dashboard mounts evidence read-only. Do not run DDL or WAL pragmas on a
    # catalog that is being consumed as an immutable read model.
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            rows = connection.execute(
                "SELECT * FROM evidence_bundles ORDER BY generated_at DESC LIMIT ?",
                (max(1, min(int(limit), 5_000)),),
            ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def reindex_bundles(root: Path | str, index_path: Path | str) -> dict[str, Any]:
    indexed = invalid = 0
    for path in sorted(Path(root).glob("veb_*")):
        result = verify_bundle(path)
        if not result.valid:
            invalid += 1
            continue
        manifest, _ = load_bundle(path, verify=False)
        index_bundle(manifest, path, index_path)
        indexed += 1
    return {"indexed": indexed, "invalid": invalid, "can_trade": False}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and verify VNEDGE evidence bundles")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--report", required=True)
    build.add_argument("--root", default="research/evidence_bundles")
    build.add_argument("--index")
    build.add_argument("--code-sha", default=os.environ.get("VNEDGE_BUILD_SHA", "UNKNOWN"))
    build.add_argument("--revision-id")
    build.add_argument("--data-sha256")
    build.add_argument("--engine-version", default=os.environ.get("VNEDGE_BUILD_SHA", "UNKNOWN"))
    build.add_argument(
        "--parity-status", choices=("EXACT", "MISMATCH", "NOT_REPORTED"),
        default="NOT_REPORTED",
    )
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", required=True)
    reindex = sub.add_parser("reindex")
    reindex.add_argument("--root", default="research/evidence_bundles")
    reindex.add_argument("--index", default="research/evidence_bundles/index.sqlite")
    listing = sub.add_parser("list")
    listing.add_argument("--index", default="research/evidence_bundles/index.sqlite")
    listing.add_argument("--limit", type=int, default=100)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "build":
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        manifest = publish_backtest_bundle(
            report,
            root=args.root,
            index_path=args.index,
            code_sha=args.code_sha,
            revision_id=args.revision_id,
            data_sha256=args.data_sha256,
            engine_version=args.engine_version,
            parity_status=args.parity_status,
        )
        payload: Any = manifest.model_dump(mode="json")
    elif args.command == "verify":
        payload = verify_bundle(args.bundle).model_dump(mode="json")
    elif args.command == "reindex":
        payload = reindex_bundles(args.root, args.index)
    else:
        payload = list_bundle_index(args.index, limit=args.limit)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
