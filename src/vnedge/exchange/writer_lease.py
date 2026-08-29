"""Process-lifetime ownership lease for canonical public-trade writers.

The candle lake is single-writer by contract.  A deployment may run either
the legacy ``pulse-recorder`` process or the integrated lane producer, never
both.  This advisory lock turns that topology rule into a fail-closed runtime
check instead of relying on a compose comment or operator memory.
"""

from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType
from typing import Self, TextIO


class CanonicalWriterLeaseError(RuntimeError):
    """Another process already owns the canonical writer for this venue."""


class CanonicalWriterLease:
    """Hold an exclusive, non-blocking venue writer lock until shutdown."""

    def __init__(self, root: Path, exchange: str) -> None:
        normalized = exchange.strip().lower()
        if not normalized:
            raise ValueError("canonical writer exchange must not be empty")
        self.path = Path(root) / "runtime" / f"canonical-writer-{normalized}.lock"
        self._handle: TextIO | None = None

    def acquire(self) -> Self:
        if self._handle is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise CanonicalWriterLeaseError(
                f"canonical writer already active for {self.path.stem}; "
                "stop the legacy recorder before starting integrated ownership"
            ) from exc
        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
