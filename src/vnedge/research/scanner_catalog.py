"""Browsable catalogue of every scanner and the evidence behind it.

Public strategy directories rank by best profit, best Sharpe, best win rate.
That ordering is precisely the multiple-testing trap: sort a hundred variants
by in-sample profit and the top row is the luckiest curve fit, not the best
idea. Three families were killed here in one day after looking excellent on
exactly those metrics.

So this catalogue leads with EVIDENCE STATE, not performance:

* what stage each scanner has actually reached (untested -> selection ->
  sealed), because a sealed FAIL outranks an unsealed win as information;
* GROSS per trade before costs, because every family that died, died there,
  and net can be rescued by cost assumptions while gross cannot;
* which windows are burned, so nobody re-runs a decision on spent data.

Read-only. Assembled from the strategy registry, the hash-chained burn
registry and the pre-registration documents -- never from a hand-kept list
that can drift from what the code and the ledger say.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

#: DISPLAY order, worst to best: what an operator should see at the top of a
#: descending sort. Separate from AUTHORITY below, and conflating the two is a
#: real bug -- it left sealed failures reading as "untested" because untested
#: sits mid-table here.
EVIDENCE_ORDER: tuple[str, ...] = (
    "killed",
    "sealed_fail",
    "selection_fail",
    "exploratory_negative",
    "untested",
    "exploratory_positive",
    "selection_pass",
    "sealed_pass",
)

#: AUTHORITY: how much weight a state carries as a claim about the scanner.
#: A sealed FAIL is a STRONGER statement than no evidence at all, so any
#: verdict must displace "untested" regardless of whether it flatters.
EVIDENCE_AUTHORITY: dict[str, int] = {
    "untested": 0,
    "exploratory_negative": 1,
    "exploratory_positive": 1,
    "selection_fail": 2,
    "selection_pass": 2,
    "sealed_fail": 3,
    "sealed_pass": 3,
    "killed": 4,
}

_VERDICT_TO_STATE = {
    "FAIL": "sealed_fail",
    "REJECT": "sealed_fail",
    "FAIL_ON_SELECTION": "selection_fail",
    "EXPLORATORY_NEGATIVE": "exploratory_negative",
    "EXPLORATORY_POSITIVE_NOT_SIGNIFICANT": "exploratory_positive",
    "PASS": "sealed_pass",
}


@dataclass(frozen=True, slots=True)
class BurnedWindow:
    start: str
    end: str
    verdict: str
    kind: str


@dataclass
class ScannerEntry:
    strategy_id: str
    evidence: str = "untested"
    capital_eligible: bool = False
    shadow_eligible: bool = False
    research_only: bool = False
    killed: bool = False
    preregistrations: list[str] = field(default_factory=list)
    burned_windows: list[BurnedWindow] = field(default_factory=list)
    judgments: int = 0
    note: str = ""

    @property
    def rank(self) -> int:
        try:
            return EVIDENCE_ORDER.index(self.evidence)
        except ValueError:
            return 0


def _best_evidence(current: str, candidate: str) -> str:
    """A killed scanner stays killed; otherwise the strongest verdict wins.

    'Strongest' is not 'most flattering': a sealed FAIL is a stronger claim
    about a scanner than an untested hope, and it is ordered accordingly.
    """
    if "killed" in (current, candidate):
        return "killed"
    a = EVIDENCE_AUTHORITY.get(current, 0)
    b = EVIDENCE_AUTHORITY.get(candidate, 0)
    if b > a:
        return candidate
    if b < a:
        return current
    # Equal authority: the unfavourable reading wins. Two runs at the same
    # stage disagreeing is not licence to quote the better one.
    return candidate if candidate.endswith("_fail") or candidate.endswith("_negative") else current


def build_catalog(
    *,
    strategies: Iterable[str],
    capital_approved: Iterable[str],
    research_only: Iterable[str],
    shadow_observe: Iterable[str],
    killed: Iterable[str],
    burn_records: Sequence[dict],
    prereg_dir: Path | None = None,
) -> list[ScannerEntry]:
    """One row per known scanner, richest evidence first."""
    capital = set(capital_approved)
    research = set(research_only)
    shadow = set(shadow_observe)
    dead = set(killed)

    known = set(strategies) | capital | research | shadow | dead
    known |= {r.get("strategy_id") for r in burn_records if r.get("strategy_id")}

    entries: dict[str, ScannerEntry] = {
        sid: ScannerEntry(
            strategy_id=sid,
            evidence="killed" if sid in dead else "untested",
            capital_eligible=sid in capital,
            shadow_eligible=sid in shadow,
            research_only=sid in research,
            killed=sid in dead,
        )
        for sid in sorted(known)
    }

    for record in burn_records:
        sid = record.get("strategy_id")
        entry = entries.get(sid)
        if entry is None:
            continue
        verdict = str(record.get("verdict", ""))
        entry.evidence = _best_evidence(
            entry.evidence, _VERDICT_TO_STATE.get(verdict, "exploratory_negative")
        )
        entry.judgments += 1
        entry.burned_windows.append(BurnedWindow(
            start=str(record.get("window_start", "")),
            end=str(record.get("window_end", "")),
            verdict=verdict,
            kind=str(record.get("kind", "")),
        ))
        if not entry.note:
            entry.note = str(record.get("note", ""))[:400]

    if prereg_dir is not None and Path(prereg_dir).is_dir():
        for path in sorted(Path(prereg_dir).glob("*.md")):
            for entry in entries.values():
                if entry.strategy_id.rsplit("_v", 1)[0] in path.stem:
                    entry.preregistrations.append(path.name)

    return sorted(
        entries.values(),
        key=lambda e: (-e.rank, -e.judgments, e.strategy_id),
    )


def catalog_payload(entries: Sequence[ScannerEntry]) -> dict[str, Any]:
    """Serialisable catalogue plus the counts an operator reads first."""
    by_state: dict[str, int] = {}
    for entry in entries:
        by_state[entry.evidence] = by_state.get(entry.evidence, 0) + 1
    return {
        "count": len(entries),
        "capital_approved": sum(1 for e in entries if e.capital_eligible),
        "by_evidence": by_state,
        "burned_windows": sum(len(e.burned_windows) for e in entries),
        "scanners": [
            {**asdict(entry), "rank": entry.rank} for entry in entries
        ],
    }


def live_catalog(prereg_dir: Path | None = Path("docs/prereg")) -> dict[str, Any]:
    """Catalogue assembled from this checkout's registry and ledger."""
    from vnedge.research.data_burn import read_records
    from vnedge.strategy import strategy_registry as reg

    try:
        records = read_records()
    except Exception:
        records = []
    return catalog_payload(build_catalog(
        strategies=getattr(reg, "STRATEGIES", ()),
        capital_approved=getattr(reg, "CAPITAL_APPROVED", ()),
        research_only=getattr(reg, "RESEARCH_ONLY", ()),
        shadow_observe=getattr(reg, "SHADOW_OBSERVE", ()),
        killed=getattr(reg, "KILLED", ()),
        burn_records=records,
        prereg_dir=prereg_dir,
    ))
