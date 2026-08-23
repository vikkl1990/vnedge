"""Immutable strategy lineage and evidence workflow.

The useful part of a public strategy platform is not its leaderboard.  It is
the workflow behind it: every strategy revision is identifiable, forks retain
their parent, reports name the engine that produced them, and suspicious runs
can be quarantined without rewriting history.

VNEDGE adds stricter boundaries:

* a revision is an immutable snapshot of code/config identity;
* a parameter or mechanism change requires a new registered strategy ID;
* events are append-only and hash chained;
* rolling research is never promotion evidence;
* only an untouched judgment PASS may be labelled promotable by the existing
  experiment index;
* this module can neither grant shadow/capital permission nor place an order.

The dashboard consumes :func:`build_strategy_workflow` as a read-only view.
Mutation is deliberately CLI/library-only so the dashboard cannot become a
back door around the reviewed strategy registry.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from vnedge.execution.fill_ledger import _GENESIS, _record_hash, verify_chain
from vnedge.research.data_burn import DEFAULT_REGISTRY_PATH as DEFAULT_BURN_REGISTRY
from vnedge.research.experiment_index import (
    DEFAULT_FEED,
    DEFAULT_PAPER_TRIALS_DIR,
    KIND_UNTOUCHED_JUDGMENT,
    RunRecord,
    build_experiment_index,
)

WORKFLOW_ID = "strategy_workflow_v1"
DEFAULT_WORKFLOW_REGISTRY = Path("research/strategy_workflow/registry.jsonl")
DEFAULT_WORKFLOW_OUT = Path("research/live_research/strategy_workflow_latest.json")
DEFAULT_DASHBOARD_FEED_RECORD_LIMIT = 5_000

EVENT_REGISTERED = "revision_registered"
EVENT_FORKED = "revision_forked"
EVENT_QUARANTINED = "revision_quarantined"
EVENT_RETIRED = "revision_retired"
EVENT_PARITY = "engine_parity"
EVENTS = frozenset(
    {
        EVENT_REGISTERED,
        EVENT_FORKED,
        EVENT_QUARANTINED,
        EVENT_RETIRED,
        EVENT_PARITY,
    }
)

ParityStatus = Literal["PASS", "FAIL", "NOT_REPORTED"]
Visibility = Literal["private", "team", "public"]


class WorkflowError(ValueError):
    """A revision/event violates the immutable workflow contract."""


@dataclass(frozen=True, slots=True)
class StrategyRevision:
    revision_id: str
    strategy_id: str
    version: str
    mechanism: str
    timeframes: tuple[str, ...]
    symbols: tuple[str, ...]
    params: dict[str, Any]
    config_hash: str
    code_hash: str
    created_at: str
    parent_revision_id: str | None = None
    preregistration: str = ""
    backtest_engine: str = ""
    engine_version: str = ""
    visibility: Visibility = "private"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> StrategyRevision:
        return cls(
            revision_id=str(raw["revision_id"]),
            strategy_id=str(raw["strategy_id"]),
            version=str(raw["version"]),
            mechanism=str(raw.get("mechanism", "")),
            timeframes=tuple(str(v) for v in raw.get("timeframes", ())),
            symbols=tuple(str(v) for v in raw.get("symbols", ())),
            params=dict(raw.get("params", {})),
            config_hash=str(raw["config_hash"]),
            code_hash=str(raw.get("code_hash", "")),
            created_at=str(raw["created_at"]),
            parent_revision_id=(
                str(raw["parent_revision_id"])
                if raw.get("parent_revision_id")
                else None
            ),
            preregistration=str(raw.get("preregistration", "")),
            backtest_engine=str(raw.get("backtest_engine", "")),
            engine_version=str(raw.get("engine_version", "")),
            visibility=str(raw.get("visibility", "private")),  # type: ignore[arg-type]
            note=str(raw.get("note", "")),
        )


@dataclass(frozen=True, slots=True)
class RevisionState:
    revision: StrategyRevision
    status: str = "REGISTERED"
    parity_status: ParityStatus = "NOT_REPORTED"
    status_reason: str = ""
    last_event_at: str = ""
    parity: dict[str, Any] = field(default_factory=dict)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision_id(strategy_id: str, version: str, config_hash: str) -> str:
    return f"{strategy_id}@{version}+{config_hash[:10]}"


def _known_strategy_ids() -> frozenset[str]:
    from vnedge.strategy.strategy_registry import STRATEGIES

    return frozenset(STRATEGIES)


def _file_hash_for_strategy(strategy_id: str) -> str:
    from vnedge.strategy.strategy_registry import STRATEGIES

    strategy_cls = STRATEGIES.get(strategy_id)
    if strategy_cls is None:
        return ""
    source = inspect.getsourcefile(strategy_cls)
    if source is None:
        return ""
    try:
        return hashlib.sha256(Path(source).read_bytes()).hexdigest()
    except OSError:
        return ""


class StrategyWorkflowStore:
    """Append-only, tamper-evident strategy revision/event ledger."""

    def __init__(
        self,
        path: str | Path = DEFAULT_WORKFLOW_REGISTRY,
        *,
        known_strategy_ids: Collection[str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.known_strategy_ids = frozenset(
            known_strategy_ids if known_strategy_ids is not None else _known_strategy_ids()
        )
        self.records = 0
        self._prev_hash = _GENESIS
        self._resume()

    def _resume(self) -> None:
        report = verify_chain(self.path)
        if not report.ok:
            raise WorkflowError(
                f"strategy workflow {self.path} fails chain verification at line "
                f"{report.first_bad_line}; refusing to append"
            )
        self.records = report.records
        if not report.records:
            return
        last: str | None = None
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = line
        if last is not None:
            self._prev_hash = str(json.loads(last)["hash"])

    def _append(self, event: dict[str, Any]) -> str:
        if event.get("event") not in EVENTS:
            raise WorkflowError(f"unknown strategy workflow event: {event.get('event')!r}")
        payload = {**event, "seq": self.records}
        digest = _record_hash(payload, self._prev_hash)
        record = {**payload, "prev_hash": self._prev_hash, "hash": digest}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.records += 1
        self._prev_hash = digest
        return digest

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                record.pop("hash", None)
                record.pop("prev_hash", None)
                record.pop("seq", None)
                out.append(record)
        return out

    def states(self) -> dict[str, RevisionState]:
        states: dict[str, RevisionState] = {}
        for event in self.events():
            kind = event["event"]
            revision_id = str(event["revision_id"])
            if kind in {EVENT_REGISTERED, EVENT_FORKED}:
                revision = StrategyRevision.from_dict(event["revision"])
                states[revision_id] = RevisionState(
                    revision=revision,
                    last_event_at=str(event["recorded_at"]),
                )
                continue
            current = states.get(revision_id)
            if current is None:
                raise WorkflowError(f"event references unknown revision {revision_id}")
            if kind == EVENT_QUARANTINED:
                states[revision_id] = RevisionState(
                    revision=current.revision,
                    status="QUARANTINED",
                    parity_status=current.parity_status,
                    status_reason=str(event.get("reason", "")),
                    last_event_at=str(event["recorded_at"]),
                    parity=current.parity,
                )
            elif kind == EVENT_RETIRED:
                states[revision_id] = RevisionState(
                    revision=current.revision,
                    status="RETIRED",
                    parity_status=current.parity_status,
                    status_reason=str(event.get("reason", "")),
                    last_event_at=str(event["recorded_at"]),
                    parity=current.parity,
                )
            elif kind == EVENT_PARITY:
                parity = str(event.get("status", "NOT_REPORTED"))
                status = "QUARANTINED" if parity == "FAIL" else current.status
                states[revision_id] = RevisionState(
                    revision=current.revision,
                    status=status,
                    parity_status=parity,  # type: ignore[arg-type]
                    status_reason=(
                        str(event.get("reason", ""))
                        if parity == "FAIL"
                        else current.status_reason
                    ),
                    last_event_at=str(event["recorded_at"]),
                    parity=dict(event),
                )
        return states

    def register(
        self,
        *,
        strategy_id: str,
        version: str,
        mechanism: str,
        timeframes: Sequence[str],
        symbols: Sequence[str],
        params: Mapping[str, Any],
        code_hash: str | None = None,
        parent_revision_id: str | None = None,
        preregistration: str = "",
        backtest_engine: str = "",
        engine_version: str = "",
        visibility: Visibility = "private",
        note: str = "",
        now: datetime | None = None,
        event_kind: str = EVENT_REGISTERED,
    ) -> StrategyRevision:
        if strategy_id not in self.known_strategy_ids:
            raise WorkflowError(
                f"strategy {strategy_id!r} is not in the reviewed strategy registry"
            )
        if not version.strip() or not mechanism.strip():
            raise WorkflowError("version and mechanism are required")
        if not timeframes or not all(str(v).strip() for v in timeframes):
            raise WorkflowError("at least one non-empty timeframe is required")
        if not symbols or not all(str(v).strip() for v in symbols):
            raise WorkflowError("at least one non-empty symbol is required")
        if visibility not in {"private", "team", "public"}:
            raise WorkflowError(f"invalid visibility {visibility!r}")
        states = self.states()
        if parent_revision_id is not None and parent_revision_id not in states:
            raise WorkflowError(f"unknown parent revision {parent_revision_id}")
        config = {
            "strategy_id": strategy_id,
            "version": version,
            "mechanism": mechanism,
            "timeframes": sorted({str(v) for v in timeframes}),
            "symbols": sorted({str(v) for v in symbols}),
            "params": dict(params),
        }
        config_hash = _canonical_hash(config)
        revision_id = _revision_id(strategy_id, version, config_hash)
        if revision_id in states:
            raise WorkflowError(f"revision already exists: {revision_id}")
        if any(
            state.revision.strategy_id == strategy_id
            and state.revision.version == version
            for state in states.values()
        ):
            raise WorkflowError(
                f"{strategy_id} version {version} already exists; versions are immutable"
            )
        created_at = (now or datetime.now(UTC)).isoformat()
        revision = StrategyRevision(
            revision_id=revision_id,
            strategy_id=strategy_id,
            version=version,
            mechanism=mechanism,
            timeframes=tuple(config["timeframes"]),
            symbols=tuple(config["symbols"]),
            params=dict(params),
            config_hash=config_hash,
            code_hash=code_hash if code_hash is not None else _file_hash_for_strategy(strategy_id),
            created_at=created_at,
            parent_revision_id=parent_revision_id,
            preregistration=preregistration,
            backtest_engine=backtest_engine,
            engine_version=engine_version,
            visibility=visibility,
            note=note,
        )
        self._append(
            {
                "event": event_kind,
                "revision_id": revision_id,
                "recorded_at": created_at,
                "revision": revision.to_dict(),
                "can_trade": False,
                "can_promote": False,
            }
        )
        return revision

    def fork(
        self,
        *,
        parent_revision_id: str,
        child_strategy_id: str,
        version: str,
        params: Mapping[str, Any] | None = None,
        mechanism: str | None = None,
        timeframes: Sequence[str] | None = None,
        symbols: Sequence[str] | None = None,
        note: str = "",
        now: datetime | None = None,
    ) -> StrategyRevision:
        parent = self.states().get(parent_revision_id)
        if parent is None:
            raise WorkflowError(f"unknown parent revision {parent_revision_id}")
        if child_strategy_id == parent.revision.strategy_id:
            raise WorkflowError(
                "a behavioral fork requires a new registered strategy_id; "
                "do not mutate an existing strategy in place"
            )
        return self.register(
            strategy_id=child_strategy_id,
            version=version,
            mechanism=mechanism or parent.revision.mechanism,
            timeframes=timeframes or parent.revision.timeframes,
            symbols=symbols or parent.revision.symbols,
            params=params if params is not None else parent.revision.params,
            parent_revision_id=parent_revision_id,
            preregistration="",
            backtest_engine=parent.revision.backtest_engine,
            engine_version=parent.revision.engine_version,
            visibility=parent.revision.visibility,
            note=note,
            now=now,
            event_kind=EVENT_FORKED,
        )

    def quarantine(
        self,
        revision_id: str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> None:
        state = self.states().get(revision_id)
        if state is None:
            raise WorkflowError(f"unknown revision {revision_id}")
        if state.status in {"QUARANTINED", "RETIRED"}:
            raise WorkflowError(f"revision {revision_id} is already {state.status.lower()}")
        if not reason.strip():
            raise WorkflowError("quarantine reason is required")
        self._append(
            {
                "event": EVENT_QUARANTINED,
                "revision_id": revision_id,
                "recorded_at": (now or datetime.now(UTC)).isoformat(),
                "reason": reason,
                "can_trade": False,
                "can_promote": False,
            }
        )

    def retire(
        self,
        revision_id: str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> None:
        state = self.states().get(revision_id)
        if state is None:
            raise WorkflowError(f"unknown revision {revision_id}")
        if state.status in {"QUARANTINED", "RETIRED"}:
            raise WorkflowError(f"revision {revision_id} is already {state.status.lower()}")
        if not reason.strip():
            raise WorkflowError("retirement reason is required")
        self._append(
            {
                "event": EVENT_RETIRED,
                "revision_id": revision_id,
                "recorded_at": (now or datetime.now(UTC)).isoformat(),
                "reason": reason,
                "can_trade": False,
                "can_promote": False,
            }
        )

    def record_parity(
        self,
        revision_id: str,
        status: Literal["PASS", "FAIL"],
        *,
        reference_run_id: str,
        current_run_id: str,
        max_metric_delta: float,
        reason: str = "",
        now: datetime | None = None,
    ) -> None:
        state = self.states().get(revision_id)
        if state is None:
            raise WorkflowError(f"unknown revision {revision_id}")
        if state.status in {"QUARANTINED", "RETIRED"}:
            raise WorkflowError(
                f"cannot attach parity to terminal revision {revision_id} ({state.status})"
            )
        if status == "FAIL" and not reason.strip():
            raise WorkflowError("failed parity requires an operator-visible reason")
        self._append(
            {
                "event": EVENT_PARITY,
                "revision_id": revision_id,
                "recorded_at": (now or datetime.now(UTC)).isoformat(),
                "status": status,
                "reference_run_id": reference_run_id,
                "current_run_id": current_run_id,
                "max_metric_delta": float(max_metric_delta),
                "reason": reason,
                "can_trade": False,
                "can_promote": False,
            }
        )


def _version_from_strategy_id(strategy_id: str) -> str:
    match = re.search(r"_v(\d+)$", strategy_id)
    return match.group(1) if match else "registry"


def _latest(records: Sequence[RunRecord]) -> RunRecord | None:
    return max(records, key=lambda row: (row.recorded_at, row.run_id), default=None)


def _stage(
    *,
    strategy_id: str,
    state_status: str,
    runs: Sequence[RunRecord],
    shadow: Collection[str],
    killed: Collection[str],
    has_preregistration: bool,
) -> str:
    if strategy_id in killed:
        return "KILLED"
    if state_status in {"QUARANTINED", "RETIRED"}:
        return state_status
    judgments = [r for r in runs if r.run_kind == KIND_UNTOUCHED_JUDGMENT]
    latest_judgment = _latest(judgments)
    if strategy_id in shadow:
        return "SHADOW_OBSERVE"
    if latest_judgment is not None:
        return "OOS_PASS" if latest_judgment.verdict == "PASS" else "OOS_REJECT"
    if runs:
        return "BACKTESTED"
    if has_preregistration:
        return "PREREGISTERED"
    return "REGISTERED"


def _metric(metrics: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if metrics.get(key) is not None:
            return metrics[key]
    return None


def _workflow_row(
    state: RevisionState,
    *,
    runs: Sequence[RunRecord],
    research_only: Collection[str],
    shadow: Collection[str],
    killed: Collection[str],
) -> dict[str, Any]:
    revision = state.revision
    latest_run = _latest(runs)
    latest_judgment = _latest(
        [r for r in runs if r.run_kind == KIND_UNTOUCHED_JUDGMENT]
    )
    latest_metric_run = _latest(
        [
            run
            for run in runs
            if any(
                run.metrics.get(key) is not None
                for key in (
                    "oos_trades",
                    "trades",
                    "fills",
                    "oos_net_usd",
                    "net_usd",
                    "realized_pnl_usd",
                )
            )
        ]
    )
    metrics = latest_metric_run.metrics if latest_metric_run else {}
    trades = _metric(metrics, "oos_trades", "trades", "fills")
    net = _metric(metrics, "oos_net_usd", "net_usd", "realized_pnl_usd")
    profit_factor = _metric(metrics, "profit_factor")
    max_drawdown = _metric(metrics, "max_drawdown_pct", "max_drawdown")
    sample_qualified = isinstance(trades, (int, float)) and trades >= 30
    governance_flags: list[str] = []
    if not revision.code_hash:
        governance_flags.append("CODE_HASH_MISSING")
    if latest_run is None:
        governance_flags.append("NO_BACKTEST_REPORT")
    if latest_judgment is None:
        governance_flags.append("NO_UNTOUCHED_JUDGMENT")
    if state.parity_status == "NOT_REPORTED":
        governance_flags.append("ENGINE_PARITY_NOT_REPORTED")
    if trades is not None and not sample_qualified:
        governance_flags.append("UNDER_SAMPLED")
    if state.status == "QUARANTINED":
        governance_flags.append("QUARANTINED")
    stage = _stage(
        strategy_id=revision.strategy_id,
        state_status=state.status,
        runs=runs,
        shadow=shadow,
        killed=killed,
        has_preregistration=bool(revision.preregistration),
    )
    return {
        **revision.to_dict(),
        "status": state.status,
        "stage": stage,
        "status_reason": state.status_reason,
        "parity_status": state.parity_status,
        "parity": state.parity,
        "last_event_at": state.last_event_at,
        "research_only": revision.strategy_id in research_only,
        "shadow_eligible": revision.strategy_id in shadow,
        "killed": revision.strategy_id in killed,
        "latest_run": latest_run.to_dict() if latest_run else None,
        "latest_judgment": latest_judgment.to_dict() if latest_judgment else None,
        "performance": {
            "after_cost_net_usd": net,
            "trades": trades,
            "profit_factor": profit_factor,
            "max_drawdown_pct": max_drawdown,
            "sample_qualified": sample_qualified,
        },
        "governance_flags": governance_flags,
        "can_trade": False,
        "can_promote": False,
    }


def _synthetic_states(prereg_dir: Path) -> dict[str, RevisionState]:
    from vnedge.strategy.strategy_registry import STRATEGIES

    preregs = list(prereg_dir.glob("*.md")) if prereg_dir.is_dir() else []
    out: dict[str, RevisionState] = {}
    for strategy_id in sorted(STRATEGIES):
        related = next(
            (
                path
                for path in preregs
                if strategy_id.rsplit("_v", 1)[0] in path.stem
            ),
            None,
        )
        code_hash = _file_hash_for_strategy(strategy_id)
        config_hash = _canonical_hash(
            {"strategy_id": strategy_id, "source": "strategy_registry"}
        )
        revision = StrategyRevision(
            revision_id=_revision_id(strategy_id, _version_from_strategy_id(strategy_id), config_hash),
            strategy_id=strategy_id,
            version=_version_from_strategy_id(strategy_id),
            mechanism="registered strategy implementation",
            timeframes=(),
            symbols=(),
            params={},
            config_hash=config_hash,
            code_hash=code_hash,
            created_at="",
            preregistration=str(related) if related else "",
            note="derived from strategy_registry; register an explicit revision for frozen params",
        )
        out[revision.revision_id] = RevisionState(revision=revision)
    return out


def build_strategy_workflow(
    *,
    workflow_registry_path: str | Path = DEFAULT_WORKFLOW_REGISTRY,
    feed_path: str | Path = DEFAULT_FEED,
    burn_registry_path: str | Path = DEFAULT_BURN_REGISTRY,
    paper_trials_dir: str | Path = DEFAULT_PAPER_TRIALS_DIR,
    prereg_dir: str | Path = Path("docs/prereg"),
    feed_max_records: int | None = DEFAULT_DASHBOARD_FEED_RECORD_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Join lineage, engine parity, run evidence, and runtime permissions.

    The workflow is a control-plane view, not an exhaustive research export.
    Its rolling feed input is therefore tail-bounded by default so an
    append-only production feed cannot stall the dashboard. Offline callers
    that truly need the complete history may pass ``feed_max_records=None``.
    """
    from vnedge.strategy.strategy_registry import KILLED, RESEARCH_ONLY, SHADOW_OBSERVE

    store = StrategyWorkflowStore(workflow_registry_path)
    explicit = store.states()
    states = _synthetic_states(Path(prereg_dir))
    explicit_strategies = {state.revision.strategy_id for state in explicit.values()}
    states = {
        revision_id: state
        for revision_id, state in states.items()
        if state.revision.strategy_id not in explicit_strategies
    }
    states.update(explicit)
    experiment = build_experiment_index(
        feed_path=Path(feed_path),
        burn_registry_path=Path(burn_registry_path),
        paper_trials_dir=Path(paper_trials_dir),
        feed_max_records=feed_max_records,
        now=now,
    )
    records = [RunRecord(**row) for row in experiment["records"]]
    rows: list[dict[str, Any]] = []
    for state in states.values():
        strategy_runs = [
            record
            for record in records
            if record.strategy_id == state.revision.strategy_id
        ]
        rows.append(
            _workflow_row(
                state,
                runs=strategy_runs,
                research_only=RESEARCH_ONLY,
                shadow=SHADOW_OBSERVE,
                killed=KILLED,
            )
        )
    stage_order = {
        "QUARANTINED": 0,
        "KILLED": 1,
        "OOS_REJECT": 2,
        "SHADOW_OBSERVE": 3,
        "OOS_PASS": 4,
        "BACKTESTED": 5,
        "PREREGISTERED": 6,
        "REGISTERED": 7,
        "RETIRED": 8,
    }
    rows.sort(
        key=lambda row: (
            stage_order.get(str(row["stage"]), 99),
            str(row["strategy_id"]),
            str(row["version"]),
        )
    )
    by_stage: dict[str, int] = {}
    for row in rows:
        stage = str(row["stage"])
        by_stage[stage] = by_stage.get(stage, 0) + 1
    generated = (now or datetime.now(UTC)).isoformat()
    evidence_as_of = max(
        (record.recorded_at for record in records if record.recorded_at),
        default=None,
    )
    return {
        "workflow_id": WORKFLOW_ID,
        "generated_at": generated,
        "evidence_as_of": evidence_as_of,
        "provenance": {
            "assembled_at": generated,
            "evidence_as_of": evidence_as_of,
            "feed_max_records": feed_max_records,
            "explicit_registry_events": len(explicit),
        },
        "summary": {
            "revisions": len(rows),
            "explicit_revisions": len(explicit),
            "strategies": len({row["strategy_id"] for row in rows}),
            "by_stage": by_stage,
            "quarantined": sum(row["stage"] == "QUARANTINED" for row in rows),
            "shadow_observe": sum(row["stage"] == "SHADOW_OBSERVE" for row in rows),
            "oos_pass": sum(row["stage"] == "OOS_PASS" for row in rows),
        },
        "stages": [
            "REGISTERED",
            "PREREGISTERED",
            "BACKTESTED",
            "OOS_PASS",
            "SHADOW_OBSERVE",
            "QUARANTINED_OR_KILLED",
        ],
        "revisions": rows,
        "experiment_summary": experiment["summary"],
        "evidence_scope": {
            "rolling_feed_record_limit": feed_max_records,
            "untouched_judgments": "complete burn registry",
            "paper_trials": "complete reports directory",
        },
        "policy": {
            "immutable_revisions": True,
            "fork_requires_new_registered_strategy_id": True,
            "engine_parity_failure_quarantines_revision": True,
            "rolling_research_can_promote": False,
            "untouched_judgment_pass_required": True,
            "dashboard_is_read_only": True,
            "can_trade": False,
            "can_promote": False,
        },
    }


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        dir=path.parent,
        prefix=path.name,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        temporary = Path(handle.name)
    temporary.replace(path)
    path.chmod(0o644)


def _json_mapping(raw: str) -> dict[str, Any]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise WorkflowError("--params must be a JSON object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VNEDGE immutable strategy workflow")
    parser.add_argument("--registry", default=str(DEFAULT_WORKFLOW_REGISTRY))
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register")
    register.add_argument("--strategy", required=True)
    register.add_argument("--version", required=True)
    register.add_argument("--mechanism", required=True)
    register.add_argument("--timeframe", action="append", required=True)
    register.add_argument("--symbol", action="append", required=True)
    register.add_argument("--params", default="{}")
    register.add_argument("--preregistration", default="")
    register.add_argument("--engine", default="")
    register.add_argument("--engine-version", default="")
    register.add_argument("--note", default="")

    fork = sub.add_parser("fork")
    fork.add_argument("--parent", required=True)
    fork.add_argument("--strategy", required=True)
    fork.add_argument("--version", required=True)
    fork.add_argument("--params", default=None)
    fork.add_argument("--note", default="")

    quarantine = sub.add_parser("quarantine")
    quarantine.add_argument("--revision", required=True)
    quarantine.add_argument("--reason", required=True)

    parity = sub.add_parser("parity")
    parity.add_argument("--revision", required=True)
    parity.add_argument("--status", choices=("PASS", "FAIL"), required=True)
    parity.add_argument("--reference-run", required=True)
    parity.add_argument("--current-run", required=True)
    parity.add_argument("--max-delta", type=float, required=True)
    parity.add_argument("--reason", default="")

    build = sub.add_parser("build")
    build.add_argument("--out", default=str(DEFAULT_WORKFLOW_OUT))

    sub.add_parser("verify")
    sub.add_parser("list")

    args = parser.parse_args(argv)
    store = StrategyWorkflowStore(args.registry)
    if args.command == "register":
        revision = store.register(
            strategy_id=args.strategy,
            version=args.version,
            mechanism=args.mechanism,
            timeframes=args.timeframe,
            symbols=args.symbol,
            params=_json_mapping(args.params),
            preregistration=args.preregistration,
            backtest_engine=args.engine,
            engine_version=args.engine_version,
            note=args.note,
        )
        print(json.dumps(revision.to_dict(), indent=2, sort_keys=True))
    elif args.command == "fork":
        revision = store.fork(
            parent_revision_id=args.parent,
            child_strategy_id=args.strategy,
            version=args.version,
            params=_json_mapping(args.params) if args.params is not None else None,
            note=args.note,
        )
        print(json.dumps(revision.to_dict(), indent=2, sort_keys=True))
    elif args.command == "quarantine":
        store.quarantine(args.revision, args.reason)
    elif args.command == "parity":
        store.record_parity(
            args.revision,
            args.status,
            reference_run_id=args.reference_run,
            current_run_id=args.current_run,
            max_metric_delta=args.max_delta,
            reason=args.reason,
        )
    elif args.command == "build":
        payload = build_strategy_workflow(workflow_registry_path=args.registry)
        _atomic_write(Path(args.out), payload)
        print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    elif args.command == "verify":
        report = verify_chain(args.registry)
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
        return 0 if report.ok else 1
    elif args.command == "list":
        print(
            json.dumps(
                [asdict(state) for state in store.states().values()],
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
