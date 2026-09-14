"""Explicit discovery of accounting lanes; infrastructure journals are not trades."""
from __future__ import annotations

from pathlib import Path

# Exact infrastructure roles, not permissive substring matching. Unknown and
# legacy lane journals remain auditable failures, never silently ignored.
INFRASTRUCTURE_JOURNALS = {
    "delta_product_specs.journal.jsonl": "venue_product_specifications",
    "shadow_portfolio.journal.jsonl": "shadow_portfolio_projection_not_paper_ledger",
}


def lane_inventory(directory: Path) -> dict:
    journals, excluded = [], []
    for path in sorted(directory.glob("*.journal.jsonl")):
        role = INFRASTRUCTURE_JOURNALS.get(path.name)
        if role:
            excluded.append({"file": path.name, "reason": role})
        else:
            journals.append(path)
    orphans = [p.name for p in sorted(directory.glob("*.fills.jsonl"))
               if not p.with_name(p.name.removesuffix(".fills.jsonl") + ".journal.jsonl").is_file()]
    return {"journals": journals, "not_label_sources": excluded,
            "orphan_fill_files": orphans, "directory_available": directory.is_dir()}
