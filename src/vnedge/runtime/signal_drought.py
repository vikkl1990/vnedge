"""Read-only signal-drought telemetry for one runtime lane.

This module is deliberately downstream of strategy evaluation.  It observes
closed-bar evaluations and execution evidence; it never returns a permission,
mutates an arm, or participates in ``signal()``.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

DroughtClass = Literal[
    "ops_silent",
    "identity_bug",
    "quote_or_cost_wait",
    "playbook_wait",
    "healthy_wait",
]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("drought event clocks must be timezone-aware")
    return value.astimezone(UTC)


def _age_seconds(now: datetime, value: datetime | None) -> float | None:
    if value is None:
        return None
    return max(0.0, (now - value).total_seconds())


@dataclass(frozen=True, slots=True)
class DroughtSnapshot:
    lane_id: str
    strategy_id: str
    symbol: str
    timeframe: str
    drought_class: DroughtClass
    last_decision_open: str | None
    last_decision_close: str | None
    last_eval_at: str | None
    last_setup_at: str | None
    last_evidence_at: str | None
    last_accept_at: str | None
    eval_age_s: float | None
    setup_age_s: float | None
    evidence_age_s: float | None
    accept_age_s: float | None
    last_decision_id: str | None
    last_primary_failed_gate: str | None
    primary_gate_counts_24h: dict[str, int]
    all_failed_gate_counts_24h: dict[str, int]
    skip_runtime: str | None
    candle_source: str
    decision_transport: str
    mreg_ready: bool | None
    structure_ready: bool | None
    quotes_armed: bool | None
    path_id: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _GateEvent:
    at: datetime
    primary: str | None
    all_failed: tuple[str, ...]


class SignalDroughtTracker:
    """Accumulate bounded lane telemetry without influencing decisions."""

    _WINDOW = timedelta(hours=24)

    def __init__(
        self,
        *,
        lane_id: str,
        strategy_id: str,
        symbol: str,
        timeframe: str,
        path_id: str,
    ) -> None:
        self.lane_id = lane_id
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.path_id = path_id
        self.last_decision_open: datetime | None = None
        self.last_decision_close: datetime | None = None
        self.last_eval_at: datetime | None = None
        self.last_setup_at: datetime | None = None
        self.last_evidence_at: datetime | None = None
        self.last_accept_at: datetime | None = None
        self.last_decision_id: str | None = None
        self.last_primary_failed_gate: str | None = None
        self.skip_runtime: str | None = None
        self.candle_source = "unknown"
        self.decision_transport = "unknown"
        self.mreg_ready: bool | None = None
        self.structure_ready: bool | None = None
        self.quotes_armed: bool | None = None
        self._last_fire_without_evidence_at: datetime | None = None
        self._gate_events: deque[_GateEvent] = deque()

    def restore(self, records: list[dict[str, object]]) -> None:
        """Rebuild telemetry from the durable WAL without projecting trades.

        The live journal is already read during lane startup for idempotency
        recovery.  Folding those same rows preserves the rolling 24-hour gate
        window across a process restart; it creates no scanner state.
        """

        for record in records:
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = str(record.get("kind") or "")
            record_at = self._parse_clock(record.get("ts"))
            if kind == "lane_eval" and self._belongs(payload, require_timeframe=True):
                opened = self._parse_clock(payload.get("bar_ts"))
                closed = self._parse_clock(payload.get("decision_at"))
                evaluated = self._parse_clock(payload.get("eval_at")) or record_at
                if opened is None or closed is None or evaluated is None or closed <= opened:
                    continue
                data_source = payload.get("data_source")
                source = data_source if isinstance(data_source, dict) else {}
                ids = payload.get("decision_ids")
                decision_ids = (
                    [str(item) for item in ids if str(item)]
                    if isinstance(ids, list)
                    else []
                )
                failed = payload.get("all_failed_gates")
                all_failed = (
                    [str(item) for item in failed]
                    if isinstance(failed, list)
                    else []
                )
                self.note_eval(
                    decision_open=opened,
                    decision_close=closed,
                    evaluated_at=evaluated,
                    eligible=bool(payload.get("eligible")),
                    fired=bool(payload.get("fired")),
                    decision_id=decision_ids[-1] if decision_ids else None,
                    primary_failed_gate=(
                        str(payload["primary_failed_gate"])
                        if payload.get("primary_failed_gate")
                        else None
                    ),
                    all_failed_gates=all_failed,
                    skip_runtime=(
                        str(payload.get("skip_reason")).split(":", 1)[0]
                        if payload.get("skip_reason")
                        else None
                    ),
                    candle_source=str(source.get("candle_source") or "unknown"),
                    decision_transport=str(source.get("decision_transport") or "unknown"),
                    mreg_ready=self._optional_bool(payload, "mreg_ready"),
                    structure_ready=self._optional_bool(payload, "structure_ready"),
                    quotes_armed=self._optional_bool(payload, "quotes_armed"),
                )
                if record_at is not None:
                    for decision_id in decision_ids:
                        self.note_evidence(decision_id=decision_id, persisted_at=record_at)
                continue
            if not self._belongs(payload, require_timeframe=False) or record_at is None:
                continue
            if kind == "decision_armed" and payload.get("decision_id"):
                self.note_evidence(
                    decision_id=str(payload["decision_id"]), persisted_at=record_at
                )
            elif kind == "scanner_transition":
                envelopes = payload.get("arm_envelopes")
                if isinstance(envelopes, list):
                    for envelope in envelopes:
                        if isinstance(envelope, dict) and envelope.get("decision_id"):
                            self.note_evidence(
                                decision_id=str(envelope["decision_id"]),
                                persisted_at=record_at,
                            )
            elif kind in {"candidate_evaluation", "risk_decision", "shadow_intent"} and bool(
                payload.get("approved")
            ):
                evidence = payload.get("execution_evidence")
                restored_decision_id = (
                    evidence.get("decision_id")
                    if isinstance(evidence, dict)
                    else payload.get("decision_id") or payload.get("intent_key")
                )
                if restored_decision_id:
                    self.note_accept(
                        decision_id=str(restored_decision_id), approved_at=record_at
                    )

    def _belongs(self, payload: dict[str, object], *, require_timeframe: bool) -> bool:
        if str(payload.get("strategy_id") or "") != self.strategy_id:
            return False
        if str(payload.get("symbol") or "") != self.symbol:
            return False
        return not (
            require_timeframe
            and str(payload.get("timeframe") or "") != self.timeframe
        )

    @staticmethod
    def _parse_clock(value: object) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            return _utc(parsed)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_bool(payload: dict[str, object], name: str) -> bool | None:
        if name not in payload or payload.get(name) is None:
            return None
        return bool(payload[name])

    def note_eval(
        self,
        *,
        decision_open: datetime,
        decision_close: datetime,
        evaluated_at: datetime,
        eligible: bool,
        fired: bool,
        decision_id: str | None,
        primary_failed_gate: str | None,
        all_failed_gates: tuple[str, ...] | list[str],
        skip_runtime: str | None,
        candle_source: str,
        decision_transport: str,
        mreg_ready: bool | None,
        structure_ready: bool | None,
        quotes_armed: bool | None,
    ) -> None:
        """Observe one completed evaluation.  No value is returned by design."""

        opened = _utc(decision_open)
        closed = _utc(decision_close)
        at = _utc(evaluated_at)
        if closed <= opened:
            raise ValueError("decision close must be after decision open")
        self.last_decision_open = opened
        self.last_decision_close = closed
        self.last_eval_at = at
        if eligible:
            # Setup time belongs to the market clock, not receipt wall time.
            self.last_setup_at = closed
        self.last_primary_failed_gate = primary_failed_gate
        self.skip_runtime = skip_runtime
        self.candle_source = candle_source
        self.decision_transport = decision_transport
        self.mreg_ready = mreg_ready
        self.structure_ready = structure_ready
        self.quotes_armed = quotes_armed
        failed = tuple(dict.fromkeys(str(item) for item in all_failed_gates if str(item)))
        self._gate_events.append(_GateEvent(at, primary_failed_gate, failed))
        if fired and not decision_id:
            self._last_fire_without_evidence_at = at
        self._prune(at)

    def note_evidence(self, *, decision_id: str, persisted_at: datetime) -> None:
        """Advance only the evidence clock; gate histograms intentionally persist."""

        decision_id = str(decision_id).strip()
        if not decision_id:
            raise ValueError("decision_id must be non-empty")
        at = _utc(persisted_at)
        self.last_decision_id = decision_id
        self.last_evidence_at = at
        if (
            self._last_fire_without_evidence_at is not None
            and at >= self._last_fire_without_evidence_at
        ):
            self._last_fire_without_evidence_at = None

    def note_accept(self, *, decision_id: str, approved_at: datetime) -> None:
        """Observe an approved candidate; approval cannot manufacture evidence."""

        decision_id = str(decision_id).strip()
        if not decision_id:
            raise ValueError("accepted decision requires decision_id")
        self.last_accept_at = _utc(approved_at)

    def snapshot(self, *, now: datetime, timeframe_seconds: int | None) -> DroughtSnapshot:
        now = _utc(now)
        self._prune(now)
        eval_age = _age_seconds(now, self.last_eval_at)
        setup_age = _age_seconds(now, self.last_setup_at)
        evidence_age = _age_seconds(now, self.last_evidence_at)
        accept_age = _age_seconds(now, self.last_accept_at)
        primary_counts = Counter(
            event.primary for event in self._gate_events if event.primary is not None
        )
        all_counts: Counter[str] = Counter()
        for event in self._gate_events:
            all_counts.update(event.all_failed)
        if timeframe_seconds is None or eval_age is None or eval_age > 1.5 * timeframe_seconds:
            drought_class: DroughtClass = "ops_silent"
        elif self._last_fire_without_evidence_at is not None:
            drought_class = "identity_bug"
        elif self.last_setup_at is not None and (
            self.last_accept_at is None or self.last_setup_at > self.last_accept_at
        ):
            drought_class = "quote_or_cost_wait"
        elif evidence_age is None or evidence_age > max(float(timeframe_seconds), eval_age):
            drought_class = "playbook_wait"
        else:
            drought_class = "healthy_wait"
        return DroughtSnapshot(
            lane_id=self.lane_id,
            strategy_id=self.strategy_id,
            symbol=self.symbol,
            timeframe=self.timeframe,
            drought_class=drought_class,
            last_decision_open=(
                self.last_decision_open.isoformat() if self.last_decision_open else None
            ),
            last_decision_close=(
                self.last_decision_close.isoformat() if self.last_decision_close else None
            ),
            last_eval_at=self.last_eval_at.isoformat() if self.last_eval_at else None,
            last_setup_at=self.last_setup_at.isoformat() if self.last_setup_at else None,
            last_evidence_at=(
                self.last_evidence_at.isoformat() if self.last_evidence_at else None
            ),
            last_accept_at=self.last_accept_at.isoformat() if self.last_accept_at else None,
            eval_age_s=eval_age,
            setup_age_s=setup_age,
            evidence_age_s=evidence_age,
            accept_age_s=accept_age,
            last_decision_id=self.last_decision_id,
            last_primary_failed_gate=self.last_primary_failed_gate,
            primary_gate_counts_24h=dict(primary_counts),
            all_failed_gate_counts_24h=dict(all_counts),
            skip_runtime=self.skip_runtime,
            candle_source=self.candle_source,
            decision_transport=self.decision_transport,
            mreg_ready=self.mreg_ready,
            structure_ready=self.structure_ready,
            quotes_armed=self.quotes_armed,
            path_id=self.path_id,
        )

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._WINDOW
        while self._gate_events and self._gate_events[0].at < cutoff:
            self._gate_events.popleft()
