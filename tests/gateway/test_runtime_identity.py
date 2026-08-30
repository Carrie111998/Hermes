"""Unit tests for runtime identity verification, endpoint integration, and public redaction."""

import os
from pathlib import Path
import pytest

from agent.runtime_identity import RuntimeIdentity, get_runtime_identity


def test_runtime_identity_structure():
    ident = get_runtime_identity(public=False)
    assert "pid" in ident
    assert "process_start_time" in ident
    assert "hermes_home_digest" in ident
    assert isinstance(ident["pid"], int)
    assert ident["pid"] == os.getpid()


def test_runtime_identity_public_redaction():
    ident_pub = get_runtime_identity(public=True)
    assert "pid" in ident_pub
    assert "hermes_home_digest" in ident_pub
    # Should not contain unredacted full filesystem paths
    home_str = Path.home().as_posix()
    for k, v in ident_pub.items():
        if isinstance(v, str):
            assert not v.startswith(home_str)
            assert "/" not in v or k in ("release_tag",)


def test_runtime_identity_dataclass():
    ri = RuntimeIdentity(
        pid=123,
        process_start_time=456,
        hermes_home_digest="abc12345",
        release_tag="v2026.8.19",
        commit_sha="62b2d78025c349996e753c6f7c748de035eb8048",
    )
    full = ri.to_dict(public=False)
    assert full["commit_sha"] == "62b2d78025c349996e753c6f7c748de035eb8048"
    assert full["release_tag"] == "v2026.8.19"

    pub = ri.to_dict(public=True)
    assert pub["commit_sha"] == "62b2d780"
    assert pub["hermes_home_digest"] == "abc12345"


def test_gateway_runtime_status_contains_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    from gateway.status import _build_runtime_status_record
    record = _build_runtime_status_record()
    assert "runtime_identity" in record
    assert record["runtime_identity"]["pid"] == os.getpid()
    assert "hermes_home_digest" in record["runtime_identity"]
