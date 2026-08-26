"""Focused contracts for trusted file-slice provenance."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest


def _identity() -> dict[str, str]:
    return {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "request_id": "request-1",
        "policy_digest": "policy-1",
    }


def test_registry_issues_only_an_exact_current_canonical_slice(tmp_path: Path):
    from agent.source_provenance import SourceProvenanceRegistry

    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    approved = b"two\nthree\n"

    registry = SourceProvenanceRegistry()
    grant = registry.issue_file_slice(
        path=source,
        line_start=2,
        line_end=3,
        content=approved,
        **_identity(),
    )

    assert grant.canonical_path == source.resolve()
    assert (grant.line_start, grant.line_end) == (2, 3)
    assert grant.content_sha256 == sha256(approved).hexdigest()
    assert registry.grants_for_request("request-1") == (grant,)


def test_registry_rejects_adjacent_or_mutated_bytes(tmp_path: Path):
    from agent.source_provenance import SourceProvenanceError, SourceProvenanceRegistry

    source = tmp_path / "app.py"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    registry = SourceProvenanceRegistry()

    with pytest.raises(SourceProvenanceError, match="content_mismatch"):
        registry.issue_file_slice(
            path=source,
            line_start=2,
            line_end=2,
            content=b"one\ntwo\n",
            **_identity(),
        )

    source.write_text("one\nchanged\nthree\n", encoding="utf-8")
    with pytest.raises(SourceProvenanceError, match="content_mismatch"):
        registry.issue_file_slice(
            path=source,
            line_start=2,
            line_end=2,
            content=b"two\n",
            **_identity(),
        )

    assert registry.grants_for_request("request-1") == ()


def test_registry_fails_closed_for_symlink_and_redacted_content(tmp_path: Path):
    from agent.source_provenance import SourceProvenanceError, SourceProvenanceRegistry

    target = tmp_path / "target.py"
    target.write_text("safe\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(target)
    registry = SourceProvenanceRegistry()

    with pytest.raises(SourceProvenanceError, match="symlink"):
        registry.issue_file_slice(
            path=link,
            line_start=1,
            line_end=1,
            content=b"safe\n",
            **_identity(),
        )

    target.write_text("token=sk-12345678901234567890\n", encoding="utf-8")
    with pytest.raises(SourceProvenanceError, match="redaction"):
        registry.issue_file_slice(
            path=target,
            line_start=1,
            line_end=1,
            content=target.read_bytes(),
            **_identity(),
        )


def test_registry_keeps_grants_request_scoped_and_clearable(tmp_path: Path):
    from agent.source_provenance import SourceProvenanceRegistry

    source = tmp_path / "app.py"
    source.write_text("safe\n", encoding="utf-8")
    registry = SourceProvenanceRegistry()
    grant = registry.issue_file_slice(
        path=source,
        line_start=1,
        line_end=1,
        content=b"safe\n",
        **_identity(),
    )

    assert registry.grants_for_request("other-request") == ()
    registry.clear_request("request-1")
    assert registry.grants_for_request(grant.request_id) == ()


def test_tool_executor_records_only_opaque_trusted_read_grant_metadata(tmp_path: Path):
    from agent.source_provenance import SourceProvenanceRegistry
    from agent.tool_executor import _attach_trusted_source_provenance_metadata

    source = tmp_path / "app.py"
    source.write_text("safe\n", encoding="utf-8")
    registry = SourceProvenanceRegistry()
    registry.issue_file_slice(path=source, line_start=1, line_end=1, content=b"safe\n", **_identity())
    agent = SimpleNamespace(
        _source_provenance_registry=registry,
        _current_api_request_id="request-1",
    )

    _attach_trusted_source_provenance_metadata(agent, "read_file")

    metadata = agent._source_provenance_metadata["request-1"]
    assert len(metadata["source_grant_digests"]) == 1
    assert "safe" not in repr(metadata)

    terminal_agent = SimpleNamespace(
        _source_provenance_registry=registry,
        _current_api_request_id="request-1",
    )
    _attach_trusted_source_provenance_metadata(terminal_agent, "terminal")
    assert not hasattr(terminal_agent, "_source_provenance_metadata")
