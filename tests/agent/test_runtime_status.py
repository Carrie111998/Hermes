import json
import os
from pathlib import Path
from types import SimpleNamespace

from agent.runtime_status import emit_runtime_status
from hermes_cli._parser import build_top_level_parser


def _agent(*, used=799_123, size=1_000_000, compressions=3, session_id="role-session"):
    return SimpleNamespace(
        runtime_status_file=None,
        session_id=session_id,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=used,
            context_length=size,
            compression_count=compressions,
        ),
    )


def test_runtime_status_is_opt_in(tmp_path: Path):
    agent = _agent()
    target = tmp_path / "runtime-status.json"
    assert emit_runtime_status(agent) is False
    assert not target.exists()


def test_runtime_status_atomically_publishes_per_agent_context_and_compressions(tmp_path: Path):
    target = tmp_path / "runtime-status.json"
    agent = _agent()
    agent.runtime_status_file = str(target)

    assert emit_runtime_status(agent) is True

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": "1.0.0",
        "pid": os.getpid(),
        "session_id": "role-session",
        "context_used": 799_123,
        "context_size": 1_000_000,
        "compression_count": 3,
        "updated_at": payload["updated_at"],
    }
    assert payload["updated_at"].endswith("Z")
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".runtime-status.json.*.tmp"))


def test_runtime_status_does_not_require_os_fchmod(monkeypatch, tmp_path: Path):
    target = tmp_path / "runtime-status.json"
    agent = _agent()
    agent.runtime_status_file = str(target)
    monkeypatch.delattr(os, "fchmod")

    assert emit_runtime_status(agent) is True
    assert json.loads(target.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_runtime_status_clamps_invalid_counters_without_leaking_other_agent_state(tmp_path: Path):
    target = tmp_path / "runtime-status.json"
    agent = _agent(used=2_000_000, size=1_000_000, compressions=-4)
    agent.runtime_status_file = str(target)

    assert emit_runtime_status(agent) is True
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["context_used"] == 1_000_000
    assert payload["context_size"] == 1_000_000
    assert payload["compression_count"] == 0


def test_runtime_status_invalid_home_expansion_is_non_fatal():
    agent = _agent()
    agent.runtime_status_file = "~hermes-agent-user-that-must-not-exist/status.json"

    assert emit_runtime_status(agent) is False


def test_runtime_status_file_is_accepted_before_or_after_chat_subcommand():
    parser, _, chat_parser = build_top_level_parser()
    top = parser.parse_args(["--runtime-status-file", "/tmp/role-status.json"])
    chat = chat_parser.parse_args(["--runtime-status-file", "/tmp/role-status.json"])
    assert top.runtime_status_file == "/tmp/role-status.json"
    assert chat.runtime_status_file == "/tmp/role-status.json"
