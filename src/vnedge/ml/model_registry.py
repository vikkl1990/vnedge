"""Model registry — versioned, auditable model storage.

Two layers live here:

* The original ``save``/``load``/``list_versions`` API for ``TrainedModel``
  artifacts (unchanged — existing callers depend on it).
* A governance layer (this file's larger half): ``ModelMetadata`` + a status
  lifecycle (``research → candidate → paper → promoted → retired/rejected``),
  immutable artifacts, an append-only metadata log, and a hash-chained audit
  trail. Runtime (paper/live) may only ``load_artifact`` a model whose status
  is ``paper`` or ``promoted`` — the registry is the gate.

Nothing here trains a model or decides a trade; it stores, versions, and gates.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import joblib

from vnedge.ml.trainer import TrainedModel

ModelStatus = Literal["research", "candidate", "paper", "promoted", "retired", "rejected"]

#: Statuses a runtime (paper/live) is allowed to load. Pure research must never
#: reach an order path.
RUNTIME_LOADABLE: frozenset[str] = frozenset({"paper", "promoted"})

#: The ONLY legal status transitions. Promotion is one-directional up the ladder;
#: rejected/retired are terminal.
_TRANSITIONS: dict[str, set[str]] = {
    "research": {"candidate", "rejected"},
    "candidate": {"paper", "rejected"},
    "paper": {"promoted", "rejected", "retired"},
    "promoted": {"retired"},
    "retired": set(),
    "rejected": set(),
}


@dataclass(frozen=True)
class ModelMetadata:
    """Immutable, self-describing record of one model artifact."""

    model_id: str
    family: str
    created_at: datetime
    trained_on_window: str
    feature_set_version: str
    algorithm: str
    hyperparams: dict
    metrics: dict
    status: ModelStatus
    artifact_path: str
    config_hash: str
    notes: str = ""
    promoted_at: datetime | None = None
    retired_at: datetime | None = None

    def to_dict(self) -> dict:
        d = {
            "model_id": self.model_id,
            "family": self.family,
            "created_at": self.created_at.isoformat(),
            "trained_on_window": self.trained_on_window,
            "feature_set_version": self.feature_set_version,
            "algorithm": self.algorithm,
            "hyperparams": self.hyperparams,
            "metrics": self.metrics,
            "status": self.status,
            "artifact_path": self.artifact_path,
            "config_hash": self.config_hash,
            "notes": self.notes,
            "promoted_at": self.promoted_at.isoformat() if self.promoted_at else None,
            "retired_at": self.retired_at.isoformat() if self.retired_at else None,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ModelMetadata":
        def _dt(v: str | None) -> datetime | None:
            return datetime.fromisoformat(v) if v else None

        return cls(
            model_id=d["model_id"],
            family=d["family"],
            created_at=_dt(d["created_at"]),  # type: ignore[arg-type]
            trained_on_window=d["trained_on_window"],
            feature_set_version=d["feature_set_version"],
            algorithm=d["algorithm"],
            hyperparams=d.get("hyperparams", {}),
            metrics=d.get("metrics", {}),
            status=d["status"],
            artifact_path=d["artifact_path"],
            config_hash=d["config_hash"],
            notes=d.get("notes", ""),
            promoted_at=_dt(d.get("promoted_at")),
            retired_at=_dt(d.get("retired_at")),
        )


class ModelNotApproved(RuntimeError):
    """Raised when a non-``paper``/``promoted`` model is loaded for runtime use."""


class IllegalStatusTransition(ValueError):
    """Raised when a status change is not on the legal promotion ladder."""


class ModelRegistry:
    def __init__(self, root: Path | str = "models") -> None:
        self.root = Path(root)

    # --- legacy TrainedModel API (unchanged) -------------------------------------
    def save(self, trained: TrainedModel, metadata: dict) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        digest = hashlib.sha256(
            json.dumps(
                {"features": trained.feature_names, "params": trained.params},
                sort_keys=True, default=str,
            ).encode()
        ).hexdigest()[:8]
        version = f"hgb_{stamp}_{digest}"
        target = self.root / version
        target.mkdir(parents=True, exist_ok=False)

        joblib.dump(trained.model, target / "model.joblib")
        meta = {
            "version": version,
            "created_at": datetime.now(UTC).isoformat(),
            "feature_names": list(trained.feature_names),
            "params": trained.params,
            "train_rows": trained.train_rows,
            "positive_rate": trained.positive_rate,
            "importances": [list(kv) for kv in trained.importances],
            **metadata,
        }
        (target / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
        return version

    def load(self, version: str) -> tuple[TrainedModel, dict]:
        target = self.root / version
        meta = json.loads((target / "meta.json").read_text())
        model = joblib.load(target / "model.joblib")
        trained = TrainedModel(
            model=model,
            feature_names=tuple(meta["feature_names"]),
            params=meta["params"],
            train_rows=meta["train_rows"],
            positive_rate=meta["positive_rate"],
            importances=tuple((n, s) for n, s in meta.get("importances", [])),
        )
        return trained, meta

    def list_versions(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "meta.json").exists())

    # --- governance layer (metadata + status lifecycle) --------------------------
    @property
    def _meta_log(self) -> Path:
        return self.root / "metadata.jsonl"

    @property
    def _audit_log(self) -> Path:
        return self.root / "audit.jsonl"

    @property
    def _artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    def register(self, meta: ModelMetadata, artifact: bytes | Path) -> None:
        """Store an immutable artifact + its metadata (status must be ``research``).

        Raises if ``model_id`` already exists (artifacts are write-once).
        """
        if meta.status != "research":
            raise ValueError("newly registered models must start as status='research'")
        if self._exists(meta.model_id):
            raise FileExistsError(f"model_id already registered: {meta.model_id}")
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        dest = self._artifacts_dir / Path(meta.artifact_path).name
        if dest.exists():
            raise FileExistsError(f"artifact already exists (immutable): {dest}")
        data = artifact if isinstance(artifact, bytes) else Path(artifact).read_bytes()
        dest.write_bytes(data)
        stored = replace(meta, artifact_path=str(dest.relative_to(self.root)))
        self._append_meta(stored)
        self._append_audit(meta.model_id, None, "research", "registered")

    def get(self, model_id: str) -> ModelMetadata:
        """Latest metadata record for ``model_id`` (append-only log ⇒ last wins)."""
        latest: ModelMetadata | None = None
        for m in self._iter_meta():
            if m.model_id == model_id:
                latest = m
        if latest is None:
            raise KeyError(f"unknown model_id: {model_id}")
        return latest

    def load_artifact(self, model_id: str) -> object:
        """Deserialize the artifact — only if status is ``paper`` or ``promoted``."""
        meta = self.get(model_id)
        if meta.status not in RUNTIME_LOADABLE:
            raise ModelNotApproved(
                f"model {model_id} has status '{meta.status}'; runtime load requires "
                f"one of {sorted(RUNTIME_LOADABLE)}"
            )
        return joblib.load(self.root / meta.artifact_path)

    def list(
        self, status: ModelStatus | None = None, family: str | None = None
    ) -> list[ModelMetadata]:
        """Latest metadata per model_id, optionally filtered by status/family."""
        latest: dict[str, ModelMetadata] = {}
        for m in self._iter_meta():
            latest[m.model_id] = m
        out = list(latest.values())
        if status is not None:
            out = [m for m in out if m.status == status]
        if family is not None:
            out = [m for m in out if m.family == family]
        return sorted(out, key=lambda m: m.model_id)

    def update_status(
        self, model_id: str, new_status: ModelStatus, operator_note: str
    ) -> ModelMetadata:
        """Move a model along the legal ladder; append metadata + audit entry."""
        cur = self.get(model_id)
        if new_status not in _TRANSITIONS.get(cur.status, set()):
            raise IllegalStatusTransition(
                f"{model_id}: {cur.status} → {new_status} is not a legal transition"
            )
        now = datetime.now(UTC)
        updated = replace(
            cur,
            status=new_status,
            promoted_at=now if new_status == "promoted" else cur.promoted_at,
            retired_at=now if new_status == "retired" else cur.retired_at,
        )
        self._append_meta(updated)
        self._append_audit(model_id, cur.status, new_status, operator_note)
        return updated

    # --- storage internals -------------------------------------------------------
    def _exists(self, model_id: str) -> bool:
        return any(m.model_id == model_id for m in self._iter_meta())

    def _iter_meta(self):
        if not self._meta_log.exists():
            return
        for line in self._meta_log.read_text().splitlines():
            line = line.strip()
            if line:
                yield ModelMetadata.from_dict(json.loads(line))

    def _append_meta(self, meta: ModelMetadata) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self._meta_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(meta.to_dict()) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _append_audit(
        self, model_id: str, from_status: str | None, to_status: str, note: str
    ) -> None:
        prev = "genesis"
        if self._audit_log.exists():
            lines = self._audit_log.read_text().splitlines()
            if lines:
                prev = json.loads(lines[-1])["hash"]
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "model_id": model_id,
            "from": from_status,
            "to": to_status,
            "note": note,
            "prev_hash": prev,
        }
        entry["hash"] = hashlib.sha256(
            (json.dumps(entry, sort_keys=True) + prev).encode()
        ).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self._audit_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
