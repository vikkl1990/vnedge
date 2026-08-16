"""Operator-profile value object used by the settings service."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class OperatorProfile:
    operator_id: str
    display_name: str
    timezone: str
    created_at: datetime
    updated_at: datetime

    def public_dict(self) -> dict[str, str]:
        payload = asdict(self)
        payload["created_at"] = self.created_at.isoformat()
        payload["updated_at"] = self.updated_at.isoformat()
        return payload
