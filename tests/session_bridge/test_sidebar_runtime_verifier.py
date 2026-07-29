from __future__ import annotations

import json
from typing import Any

import pytest

from scripts.verify_sidebar_registration_runtime import (
    CONFIRMATION,
    _archive_verified_probe,
    _config_digest,
    _exposed_mcp_names,
    _mcp_names,
    _validate_mutation_gate,
    build_parser,
)


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params), timeout))
        return {}


def test_runtime_verifier_defaults_to_read_only() -> None:
    args = build_parser().parse_args([])

    assert args.apply is False
    assert args.confirm is None
    _validate_mutation_gate(args)


@pytest.mark.parametrize(
    "argv",
    (
        ["--apply"],
        ["--apply", "--confirm", "wrong"],
        ["--confirm", CONFIRMATION],
    ),
)
def test_runtime_verifier_requires_exact_apply_confirmation(
    argv: list[str],
) -> None:
    args = build_parser().parse_args(argv)

    with pytest.raises(ValueError, match="confirmation"):
        _validate_mutation_gate(args)


def test_runtime_verifier_accepts_exact_apply_confirmation() -> None:
    args = build_parser().parse_args(
        ["--apply", "--confirm", CONFIRMATION]
    )

    _validate_mutation_gate(args)


def test_runtime_verifier_extracts_only_exact_mcp_names() -> None:
    assert _mcp_names({
        "data": [
            {"name": "gbrain", "tools": {"query": {}}},
            {"name": "mempalace", "tools": {}},
        ]
    }) == ("gbrain", "mempalace")

    with pytest.raises(ValueError, match="MCP status"):
        _mcp_names({"data": [{"name": "gbrain"}, {"name": 42}]})


def test_runtime_verifier_distinguishes_disabled_from_exposed_mcp_tools() -> None:
    assert _exposed_mcp_names({
        "data": [
            {"name": "disabled", "tools": {}},
            {"name": "active", "tools": {"query": {}}},
        ]
    }) == ("active",)

    with pytest.raises(ValueError, match="MCP status"):
        _exposed_mcp_names({"data": [{"name": "malformed", "tools": []}]})


def test_runtime_verifier_hashes_config_without_returning_values() -> None:
    response = {
        "config": {
            "mcp_servers": {"private": {"env": {"TOKEN": "secret-value"}}},
            "model": "private-model",
        },
        "origins": {},
    }

    digest = _config_digest(response)

    assert len(digest) == 64
    assert "secret-value" not in digest
    assert digest == _config_digest(json.loads(json.dumps(response)))


def test_runtime_verifier_archives_only_exact_proven_thread() -> None:
    marker = "HERMES_SESSION_BRIDGE_V1:body.signature"
    response = {
        "thread": {
            "id": "thread-exact",
            "turns": [{"items": [{"type": "userMessage", "text": marker}]}],
        }
    }
    client = RecordingClient()

    _archive_verified_probe(
        client,
        response=response,
        thread_id="thread-exact",
        marker=marker,
        timeout=5.0,
    )

    assert client.calls == [
        ("thread/archive", {"threadId": "thread-exact"}, 5.0)
    ]


@pytest.mark.parametrize(
    "response",
    (
        {"thread": {"id": "other-thread", "turns": []}},
        {"thread": {"id": "thread-exact", "turns": []}},
        {"thread": {"id": "thread-exact", "turns": [{"text": "prefix marker"}]}},
    ),
)
def test_runtime_verifier_refuses_archive_without_exact_identity(
    response: dict[str, object],
) -> None:
    client = RecordingClient()

    with pytest.raises(ValueError, match="identity"):
        _archive_verified_probe(
            client,
            response=response,
            thread_id="thread-exact",
            marker="HERMES_SESSION_BRIDGE_V1:body.signature",
            timeout=5.0,
        )

    assert client.calls == []
