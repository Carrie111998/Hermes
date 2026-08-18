"""Tests for tools/agent_message_tool.py."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.agent_message_tool import (
    AGENT_MESSAGE_SCHEMA,
    _available_profiles,
    _build_agent_message_command,
    _profile_exists,
    _validate_profile_name,
    agent_message_tool,
)
from tools.registry import registry


def _make_profile(root: Path, name: str) -> None:
    if name == "default":
        home = root
    else:
        home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("model:\n  default: test\n", encoding="utf-8")


def test_validate_profile_name_rejects_shell_metacharacters():
    for bad in ["../narvi", "narvi;rm -rf /", "narvi test", "-narvi", ""]:
        try:
            _validate_profile_name(bad)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion clarity
            raise AssertionError(f"accepted unsafe profile name: {bad!r}")


def test_profile_discovery_uses_configured_profiles_root(tmp_path):
    _make_profile(tmp_path, "default")
    _make_profile(tmp_path, "narvi")

    assert _profile_exists("default", root=tmp_path)
    assert _profile_exists("narvi", root=tmp_path)
    assert _available_profiles(root=tmp_path) == ["default", "narvi"]


def test_build_command_is_argv_list_no_shell(monkeypatch):
    monkeypatch.setattr("tools.agent_message_tool._resolve_hermes_executable", lambda: "/bin/hermes")

    command = _build_agent_message_command("narvi", "hello; still one arg")

    assert command[:4] == ["/bin/hermes", "--profile", "narvi", "chat"]
    # The canonical Bot Chat session, created on first contact.
    assert command[command.index("-c") + 1] == "Bot Chat"
    assert "--create-if-missing" in command
    # The whole message is one argv element — no shell, nothing to re-parse.
    assert command[-1] == "hello; still one arg"
    assert command[-2] == "-q"


def test_agent_message_sync_success(monkeypatch, tmp_path):
    _make_profile(tmp_path, "narvi")
    monkeypatch.setattr("tools.agent_message_tool.get_default_hermes_root", lambda: tmp_path)
    monkeypatch.setattr("tools.agent_message_tool._resolve_hermes_executable", lambda: "/bin/hermes")

    completed = SimpleNamespace(
        returncode=0,
        stdout="session_id: 20260529_test\nNarvi reply\n",
        stderr="",
    )

    with patch("tools.agent_message_tool.subprocess.run", return_value=completed) as run_mock:
        result = json.loads(
            agent_message_tool(
                {
                    "to": "narvi",
                    "message": "Can you hear me?",
                    "timeout_seconds": 12,
                }
            )
        )

    assert result["success"] is True
    assert result["agent"] == "narvi" and result["peer"] is None
    assert result["session_id"] == "20260529_test"
    assert result["reply"] == "Narvi reply"
    run_mock.assert_called_once()
    command = run_mock.call_args.args[0]
    kwargs = run_mock.call_args.kwargs
    assert isinstance(command, list)
    assert kwargs["timeout"] == 12
    assert "shell" not in kwargs
    assert command[-1].endswith("Can you hear me?")
    assert command[-1].startswith("Message from")  # attribution is signed for us


def test_agent_message_timeout_returns_partial_output(monkeypatch, tmp_path):
    _make_profile(tmp_path, "narvi")
    monkeypatch.setattr("tools.agent_message_tool.get_default_hermes_root", lambda: tmp_path)
    monkeypatch.setattr("tools.agent_message_tool._resolve_hermes_executable", lambda: "/bin/hermes")

    exc = subprocess.TimeoutExpired(
        cmd=["/bin/hermes"],
        timeout=1,
        output="partial stdout",
        stderr="partial stderr",
    )
    with patch("tools.agent_message_tool.subprocess.run", side_effect=exc):
        result = json.loads(
            agent_message_tool(
                {
                    "to": "narvi",
                    "message": "slow request",
                    "timeout_seconds": 1,
                }
            )
        )

    assert result["success"] is False
    assert "timed out" in result["error"]
    assert "partial stdout" in result["partial_output"]
    assert "partial stderr" in result["partial_output"]


def test_agent_message_missing_profile_reports_available(monkeypatch, tmp_path):
    _make_profile(tmp_path, "aragorn")
    monkeypatch.setattr("tools.agent_message_tool.get_default_hermes_root", lambda: tmp_path)

    result = json.loads(agent_message_tool({"to": "narvi", "message": "hello"}))

    assert result["success"] is False
    assert "No teammate agent named" in result["error"]
    assert "aragorn" in result["available_agents"]


def test_agent_message_registered_in_messaging_toolset():
    from toolsets import TOOLSETS, resolve_toolset

    entry = registry.get_entry("agent_message")

    assert entry is not None
    assert entry.toolset == "messaging"
    assert entry.schema is AGENT_MESSAGE_SCHEMA
    assert "agent_message" in TOOLSETS["messaging"]["tools"]
    assert "agent_message" in resolve_toolset("messaging")


# ---------------------------------------------------------------------------
# The bug this tool exists to close
# ---------------------------------------------------------------------------

import shlex  # noqa: E402

import pytest  # noqa: E402

from tools.agent_message_tool import (  # noqa: E402
    _build_peer_message_command,
    _parse_target,
    attribution_prefix,
)

# The exact recipe the Bot Mode protocol section used to teach the model.
_OLD_SHELL_RECIPE = (
    'hermes -p {who} chat --in ~ -c "Bot Chat" --create-if-missing -Q -q '
    '"Message from 🤖 {me} (@{me}): {msg}"'
)

# Two distinct failure modes, tested the way each actually manifests.
_TRUNCATING = ['he said "ship it" today', 'a "b c']       # quote ends the word
_EXECUTING = ["check $(echo PWNED) now", "check `echo PWNED` now"]
_HOSTILE = _TRUNCATING + _EXECUTING


@pytest.mark.parametrize("msg", _TRUNCATING)
def test_the_old_shell_recipe_truncates_at_a_quote(msg):
    """``shlex`` models the shell's word-splitting, so this is what sh sees."""
    line = _OLD_SHELL_RECIPE.format(who="bob", me="alice", msg=msg)
    try:
        delivered = shlex.split(line)[-1]
    except ValueError:
        return  # unbalanced: the command does not even parse
    assert not delivered.endswith(msg), (
        "expected the shell recipe to mangle this message; it survived, so "
        "it is not a truncation case"
    )


@pytest.mark.parametrize("msg", _EXECUTING)
def test_the_old_shell_recipe_executes_substitutions(msg, tmp_path):
    """A real shell, because shlex does not model expansion at all.

    The stub stands in for ``hermes`` and prints the argument that reached
    ``-q``; PWNED appearing there means the sender's shell ran the payload.
    """
    stub = tmp_path / "hermes"
    stub.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do [ \"$1\" = -q ] && { printf '%s' \"$2\"; exit 0; }; shift; done\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    line = _OLD_SHELL_RECIPE.format(who="bob", me="alice", msg=msg).replace(
        "hermes ", f"{stub} ", 1
    )
    out = subprocess.run(["sh", "-c", line], capture_output=True, text=True).stdout
    assert "PWNED" in out and "echo PWNED" not in out, (
        f"expected the shell to expand the payload; -q received {out!r}"
    )


class TestAttribution:
    def test_the_prefix_is_applied_by_the_tool(self, monkeypatch, tmp_path):
        _make_profile(tmp_path, "narvi")
        monkeypatch.setattr(
            "tools.agent_message_tool.get_default_hermes_root", lambda: tmp_path
        )
        monkeypatch.setattr(
            "tools.agent_message_tool._resolve_hermes_executable", lambda: "/bin/hermes"
        )
        monkeypatch.setattr("tools.agent_message_tool.sender_handle", lambda: "alice")
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with patch("tools.agent_message_tool.subprocess.run", return_value=completed) as run:
            agent_message_tool({"to": "narvi", "message": "disk status?"})

        assert run.call_args.args[0][-1] == "Message from 🤖 alice (@alice): disk status?"

    def test_the_prefix_matches_the_one_the_protocol_documents(self):
        """A receiving agent keys off this string; drift breaks recognition."""
        from tools import bot_mode_probe

        assert attribution_prefix("dixie").startswith("Message from 🤖 dixie (@dixie):")
        assert bot_mode_probe._PROTOCOL_HEADING == "## Messaging other agents"


class TestTargetParsing:
    @pytest.mark.parametrize(
        "raw,agent,peer",
        [
            ("narvi", "narvi", None),
            ("spark/researcher", "researcher", "spark"),
        ],
    )
    def test_local_and_peer_forms(self, raw, agent, peer, monkeypatch, tmp_path):
        _make_profile(tmp_path, "narvi")
        monkeypatch.setattr(
            "tools.agent_message_tool.get_default_hermes_root", lambda: tmp_path
        )
        monkeypatch.setattr("tools.agent_message_tool.peer_names", lambda: [])
        assert _parse_target(raw) == (agent, peer)

    def test_a_bare_name_prefers_a_local_profile_over_a_peer(self, monkeypatch, tmp_path):
        _make_profile(tmp_path, "spark")
        monkeypatch.setattr(
            "tools.agent_message_tool.get_default_hermes_root", lambda: tmp_path
        )
        monkeypatch.setattr("tools.agent_message_tool.peer_names", lambda: ["spark"])
        assert _parse_target("spark") == ("spark", None)

    def test_a_bare_name_falls_through_to_a_registered_peer(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "tools.agent_message_tool.get_default_hermes_root", lambda: tmp_path
        )
        monkeypatch.setattr("tools.agent_message_tool.peer_names", lambda: ["spark"])
        assert _parse_target("spark") == ("spark", "spark")

    @pytest.mark.parametrize(
        "bad", ["", "   ", "../etc", "a;rm -rf /", "a b", "-lead", "a/b/c", "a/"]
    )
    def test_hostile_targets_are_refused(self, bad):
        with pytest.raises(ValueError):
            _parse_target(bad)

    def test_peer_command_passes_the_body_as_one_argument(self, monkeypatch):
        monkeypatch.setattr(
            "tools.agent_message_tool._resolve_hermes_executable", lambda: "/bin/hermes"
        )
        cmd = _build_peer_message_command("spark/researcher", 'say "hi" $(id)')
        assert cmd == [
            "/bin/hermes", "peer", "dm", "spark/researcher", 'say "hi" $(id)'
        ]


class TestGating:
    def test_the_tool_is_registered_in_the_messaging_toolset(self):
        from toolsets import TOOLSETS
        from tools.registry import registry

        assert registry.get_toolset_for_tool("agent_message") == "messaging"
        assert TOOLSETS["messaging"]["tools"] == ["agent_message"]

    def test_a_plain_install_does_not_see_it(self, tmp_path, monkeypatch):
        """No Bot Mode profile → the gate is shut, so no schema footprint."""
        from tools import bot_mode_probe

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text("model:\n  default: t\n", encoding="utf-8")
        assert bot_mode_probe.is_bot_mode_install(tmp_path) is False

    def test_a_bot_managed_install_opens_the_gate(self, tmp_path):
        from tools import bot_mode_probe

        (tmp_path / "config.yaml").write_text("model:\n  default: t\n", encoding="utf-8")
        (tmp_path / "profile.yaml").write_text(
            "ui_meta:\n  hermes-bots:\n    enabled: true\n", encoding="utf-8"
        )
        assert bot_mode_probe.is_bot_mode_install(tmp_path) is True
