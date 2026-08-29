from pathlib import Path

import pytest

from vnedge.exchange.writer_lease import (
    CanonicalWriterLease,
    CanonicalWriterLeaseError,
)


def test_canonical_writer_lease_refuses_dual_process_ownership(tmp_path: Path):
    first = CanonicalWriterLease(tmp_path, "binanceusdm").acquire()
    try:
        with pytest.raises(CanonicalWriterLeaseError):
            CanonicalWriterLease(tmp_path, "BINANCEUSDM").acquire()
    finally:
        first.release()

    replacement = CanonicalWriterLease(tmp_path, "binanceusdm").acquire()
    replacement.release()
