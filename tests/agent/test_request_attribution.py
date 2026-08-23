"""LiteLLM request attribution stays opt-in, complete, and metadata-only."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
from agent.request_attribution import attach_request_attribution


def _agent(**overrides):
    values = {
        "_request_attribution_enabled": True,
        "_request_attribution_litellm_endpoints": ("http://127.0.0.1:4001",),
        "_request_attribution_call_role": "primary",
        "_request_attribution_retry_count": 0,
        "_request_attribution_stream_retry_count": 0,
        "_current_api_request_id": "turn-1:api:1",
        "provider": "custom",
        "base_url": "http://127.0.0.1:4001/v1",
        "model": "MiniMax-M3-Lightcloud",
        "platform": "telegram",
        "session_id": "session-1",
        "api_mode": "chat_completions",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _header_payload(request: dict) -> dict:
    return json.loads(request["extra_headers"]["x-litellm-spend-logs-metadata"])


def test_attribution_is_opt_in_and_route_scoped():
    request = {"model": "MiniMax-M3", "messages": []}
    disabled = attach_request_attribution(
        _agent(_request_attribution_enabled=False), request
    )
    foreign = attach_request_attribution(
        _agent(base_url="https://api.example.com/v1"), request
    )
    assert disabled == request
    assert foreign == request


def test_envelope_has_explicit_nulls_and_preserves_headers(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "swarm")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    attributed = attach_request_attribution(
        _agent(),
        {"model": "MiniMax-M3-Lightcloud", "extra_headers": {"x-existing": "yes"}},
        retry_count=2,
        streaming=True,
    )
    envelope = _header_payload(attributed)["aos"]
    assert attributed["extra_headers"]["x-existing"] == "yes"
    assert envelope["schema"] == "aos.telemetry_envelope.v1"
    assert envelope["surface"] == "hermes"
    assert envelope["interface"] == "telegram"
    assert envelope["profile"] == "swarm"
    assert envelope["session_id"] == "session-1"
    assert envelope["task_id"] is None
    assert envelope["provider_slot"] == "lightcloud"
    assert envelope["repository"] is None
    assert envelope["branch"] is None
    assert envelope["commit_sha"] is None
    assert envelope["pr_url"] is None
    assert envelope["completed_at"] is None
    assert envelope["status"] == "requested"
    assert envelope["retry_count"] == 2
    assert envelope["streaming"] is True


def test_non_mapping_existing_headers_are_replaced_safely():
    attributed = attach_request_attribution(
        _agent(),
        {"model": "MiniMax-M3-Lightcloud", "extra_headers": "invalid"},
    )
    assert set(attributed["extra_headers"]) == {
        "x-litellm-spend-logs-metadata"
    }


def test_task_git_context_and_physical_request_ids(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "task-branch", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Attribution Test"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "git@github.com:lightcloud00/attribution.git",
        ],
        check=True,
    )
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
    monkeypatch.setenv("HERMES_KANBAN_BRANCH", "task-branch")
    monkeypatch.setenv(
        "HERMES_KANBAN_PR_URL",
        "https://github.com/lightcloud00/attribution/pull/7",
    )
    first = _header_payload(
        attach_request_attribution(_agent(), {"model": "minimax-m3-oscar"})
    )["aos"]
    second = _header_payload(
        attach_request_attribution(_agent(), {"model": "MiniMax-M3"})
    )["aos"]
    assert first["task_id"] == "task-1"
    assert first["repository"] == "lightcloud00/attribution"
    assert first["branch"] == "task-branch"
    assert first["pr_url"].endswith("/pull/7")
    assert first["model"] == "minimax-m3-oscar"
    assert first["provider_slot"] == "oscar"
    assert len(first["commit_sha"]) == 40
    assert first["request_id"] != second["request_id"]
    assert first["action_id"] == second["action_id"] == "turn-1:api:1"


def test_nonstreaming_dispatch_attaches_physical_envelope(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "swarm")
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return "ok"

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    agent = _agent()
    response = _dispatch_nonstreaming_api_request(
        agent,
        {"model": agent.model, "messages": []},
        make_client=lambda *_args, **_kwargs: client,
    )
    assert response == "ok"
    assert _header_payload(captured)["aos"]["surface"] == "hermes"
