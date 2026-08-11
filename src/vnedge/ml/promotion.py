"""Promotion Service — the only legal route research → paper → live.

A thin orchestration layer over ``ModelRegistry.update_status`` (which validates
the transition and writes the hash-chained audit entry). The service captures
the *operator note* and the *pre-registered criteria / results* at each gate, so
the promotion of any model is fully reconstructable from the audit trail.

    research
      → submit_for_sealed_tail(criteria)         # records the bar, no status change
      → record_sealed_tail_result(metrics, pass) # → candidate (pass) or rejected
      → approve_for_paper(operator, note)         # → paper   (human gate)
      → approve_for_live(operator, checklist)     # → promoted (human gate)

No automatic promotion exists. Every upward move needs an explicit human call.
"""

from __future__ import annotations

from pathlib import Path

from vnedge.ml.model_registry import ModelMetadata, ModelRegistry


class PromotionError(RuntimeError):
    """Raised when a promotion step is attempted out of order or unmet."""


class PromotionService:
    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    def submit_for_sealed_tail(self, model_id: str, criteria: dict) -> None:
        """Record the pre-registered sealed-tail criteria (audit only, status
        stays ``research``). Freezing the bar before the tail is opened is the
        whole discipline — this makes it auditable."""
        meta = self.registry.get(model_id)
        if meta.status != "research":
            raise PromotionError(
                f"{model_id}: sealed-tail criteria may only be submitted for "
                f"status='research' (is '{meta.status}')"
            )
        # audit-only entry via a no-op self-transition note
        self.registry._append_audit(  # noqa: SLF001 — registry owns the audit log
            model_id, "research", "research",
            f"sealed_tail_criteria: {criteria}",
        )

    def record_sealed_tail_result(
        self, model_id: str, metrics: dict, passed: bool
    ) -> ModelMetadata:
        """Move to ``candidate`` if the pre-registered bar was met, else
        ``rejected``. The metrics are captured in the operator note."""
        note = f"sealed_tail_result passed={passed} metrics={metrics}"
        target = "candidate" if passed else "rejected"
        return self.registry.update_status(model_id, target, note)

    def approve_for_paper(self, model_id: str, operator: str, note: str) -> ModelMetadata:
        """Human gate: candidate → paper. The model becomes runtime-loadable."""
        meta = self.registry.get(model_id)
        if meta.status != "candidate":
            raise PromotionError(
                f"{model_id}: paper approval requires status='candidate' (is '{meta.status}')"
            )
        return self.registry.update_status(
            model_id, "paper", f"approve_for_paper by {operator}: {note}"
        )

    def approve_for_live(
        self, model_id: str, operator: str, checklist_path: Path | str
    ) -> ModelMetadata:
        """Human gate: paper → promoted. Requires a completed checklist artifact.

        Promotion to ``promoted`` only makes the model *eligible* for live_small;
        the existing GO_LIVE_GATE + the two independent live flags still apply."""
        meta = self.registry.get(model_id)
        if meta.status != "paper":
            raise PromotionError(
                f"{model_id}: live approval requires status='paper' (is '{meta.status}')"
            )
        path = Path(checklist_path)
        if not path.exists():
            raise PromotionError(f"{model_id}: checklist not found: {path}")
        return self.registry.update_status(
            model_id, "promoted",
            f"approve_for_live by {operator}: checklist={path}",
        )
