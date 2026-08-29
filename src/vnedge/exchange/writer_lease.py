"""Process-lifetime ownership lease for canonical public-trade writers.

The candle lake is single-writer by contract.  A deployment may run either
the legacy ``pulse-recorder`` process or the integrated lane producer, never
both.  This advisory lock turns that topology rule into a fail-closed runtime
check instead of relying on a compose comment or operator memory.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self, TextIO

INHERITED_WRITER_LEASE_FD = "VNEDGE_CANONICAL_WRITER_LEASE_FD"


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

    @property
    def fileno(self) -> int:
        """Descriptor inherited by owner-authorized maintenance children."""
        if self._handle is None:
            raise RuntimeError("canonical writer lease is not acquired")
        return self._handle.fileno()

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


@contextmanager
def canonical_write_authority(
    root: Path,
    exchange: str,
    *,
    environ: Mapping[str, str] = os.environ,
) -> Iterator[None]:
    """Require the venue writer lease for every canonical mutation command.

    The long-lived market-data owner may pass its already-locked descriptor to
    a bounded maintenance child.  The child proves that the descriptor names
    the expected lease inode before writing.  Standalone commands acquire the
    lease themselves and therefore fail closed while a live owner is active.
    """
    candidate = CanonicalWriterLease(root, exchange)
    inherited = str(environ.get(INHERITED_WRITER_LEASE_FD, "")).strip()
    if inherited:
        try:
            descriptor = int(inherited)
            descriptor_stat = os.fstat(descriptor)
            path_stat = candidate.path.stat()
        except (OSError, TypeError, ValueError) as exc:
            raise CanonicalWriterLeaseError(
                "inherited canonical writer lease descriptor is invalid"
            ) from exc
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            raise CanonicalWriterLeaseError(
                "inherited canonical writer lease does not match the target venue"
            )
        try:
            # Idempotent for the owner's inherited open-file description. If
            # a caller merely passes an unlocked descriptor, this acquires the
            # lease before mutation; if a different owner is live it fails.
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CanonicalWriterLeaseError(
                "inherited canonical writer lease is not owned by this process tree"
            ) from exc
        yield
        return
    with candidate:
        yield
