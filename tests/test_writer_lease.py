from pathlib import Path

import pytest

from vnedge.exchange.writer_lease import (
    INHERITED_WRITER_LEASE_FD,
    CanonicalWriterLease,
    CanonicalWriterLeaseError,
    canonical_write_authority,
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


def test_owner_child_may_prove_the_inherited_writer_lease(tmp_path: Path):
    owner = CanonicalWriterLease(tmp_path, "binanceusdm").acquire()
    try:
        with canonical_write_authority(
            tmp_path,
            "binanceusdm",
            environ={INHERITED_WRITER_LEASE_FD: str(owner.fileno)},
        ):
            pass
    finally:
        owner.release()


def test_inherited_writer_lease_must_match_target_inode(tmp_path: Path):
    target = CanonicalWriterLease(tmp_path, "binanceusdm")
    target.path.parent.mkdir(parents=True, exist_ok=True)
    target.path.touch()
    unrelated = tmp_path / "unrelated.lock"
    unrelated.touch()
    with (
        unrelated.open("r") as handle,
        pytest.raises(CanonicalWriterLeaseError, match="does not match"),
        canonical_write_authority(
            tmp_path,
            "binanceusdm",
            environ={INHERITED_WRITER_LEASE_FD: str(handle.fileno())},
        ),
    ):
        pass
