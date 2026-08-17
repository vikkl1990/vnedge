"""Read-only fleet policy audit for detecting stale or unsafe deployments.

The verifier consumes the dashboard's coalesced ``/state`` snapshot. It never
changes a roster or runtime flag; unsafe state produces a non-zero exit so an
operator or deployment job can stop and investigate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from vnedge.strategy.strategy_registry import (
    capital_denial_reason,
    is_shadow_observe_eligible,
)

_CAPITAL_MODES = frozenset({"paper", "live_small", "live_full"})


@dataclass(frozen=True, slots=True)
class FleetFinding:
    severity: str
    code: str
    detail: str
    lane_id: str | None = None


@dataclass(frozen=True, slots=True)
class FleetPolicyReport:
    safe: bool
    build_sha: str | None
    checked_lanes: int
    findings: tuple[FleetFinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe": self.safe,
            "build_sha": self.build_sha,
            "checked_lanes": self.checked_lanes,
            "findings": [asdict(finding) for finding in self.findings],
        }


def _capital_mode(value: object) -> bool:
    mode = str(value or "").strip().lower().split()[0] if value else ""
    return mode in _CAPITAL_MODES or mode.startswith("live_")


def _lane_rows(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = snapshot.get("lanes")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, Mapping)]
    if snapshot.get("strategy_id") or snapshot.get("mode"):
        return [snapshot]
    return []


def audit_runtime_snapshot(
    snapshot: Mapping[str, Any],
    *,
    expected_build_sha: str | None = None,
) -> FleetPolicyReport:
    """Fail closed on live enablement or a non-approved strategy in a capital lane."""
    findings: list[FleetFinding] = []
    build_sha = str(snapshot.get("build_sha") or snapshot.get("version") or "").strip() or None
    if expected_build_sha and build_sha != expected_build_sha:
        findings.append(
            FleetFinding(
                "critical",
                "build_mismatch",
                f"expected build {expected_build_sha}, running {build_sha or 'unknown'}",
            )
        )
    if bool(snapshot.get("live_trading_enabled")):
        findings.append(
            FleetFinding(
                "critical",
                "live_enabled",
                "live trading is enabled while the audited deployment posture is frozen",
            )
        )

    lanes = _lane_rows(snapshot)
    for lane in lanes:
        lane_id = str(lane.get("lane_id") or "").strip() or None
        observation_class = str(lane.get("observation_class") or "").strip().lower()
        strategy_id = str(lane.get("strategy_id") or "").strip()
        if (
            observation_class == "shadow_observe"
            or (lane_id or "").startswith("shadow_observe_")
        ) and not is_shadow_observe_eligible(strategy_id):
            findings.append(
                FleetFinding(
                    "critical",
                    "shadow_observe_strategy_denied",
                    f"shadow-observe lane uses {strategy_id or '<missing>'}",
                    lane_id,
                )
            )
        if not _capital_mode(lane.get("mode")):
            continue
        reason = capital_denial_reason(strategy_id)
        if reason is not None:
            findings.append(
                FleetFinding(
                    "critical",
                    "capital_strategy_denied",
                    f"capital lane uses {strategy_id or '<missing>'}: {reason}",
                    lane_id,
                )
            )

    runtime_control = snapshot.get("runtime_control")
    declared = snapshot.get("capital_roster_size")
    if declared is None and isinstance(runtime_control, Mapping):
        declared = runtime_control.get("capital_roster_size")
    if isinstance(declared, (int, float)) and declared > 0 and not any(
        _capital_mode(lane.get("mode")) for lane in lanes
    ):
        findings.append(
            FleetFinding(
                "critical",
                "roster_count_inconsistent",
                f"snapshot declares {declared:g} capital lanes but exposes none for audit",
            )
        )
    return FleetPolicyReport(
        safe=not any(finding.severity == "critical" for finding in findings),
        build_sha=build_sha,
        checked_lanes=len(lanes),
        findings=tuple(findings),
    )


def fetch_runtime_snapshot(url: str, token: str, *, timeout_seconds: float = 10.0) -> dict:
    if not token.strip():
        raise ValueError("DASHBOARD_TOKEN is required for fleet verification")
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token.strip()}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"fleet snapshot unavailable: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("fleet snapshot must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/state")
    parser.add_argument("--expected-build-sha", default=os.environ.get("VNEDGE_BUILD_SHA"))
    args = parser.parse_args(argv)
    try:
        snapshot = fetch_runtime_snapshot(args.url, os.environ.get("DASHBOARD_TOKEN", ""))
        report = audit_runtime_snapshot(snapshot, expected_build_sha=args.expected_build_sha)
    except (TypeError, ValueError, RuntimeError) as exc:
        print(json.dumps({"safe": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.safe else 1


if __name__ == "__main__":
    sys.exit(main())
