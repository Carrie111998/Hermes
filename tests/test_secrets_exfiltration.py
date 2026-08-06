"""Provider-egress secret exfiltration gate (#77162, #77165).

A secret applied from an external source (Bitwarden/1Password/command)
under ANY name — including non-credential-shaped names like
``DATABASE_URL`` or ``FOO`` — that a tool echoes into its result passes
all shape-based redaction passes (no vendor prefix, no secret keyword in
the key name) and would previously be transmitted VERBATIM to the model
provider in the next API call. The exact-value pass
(``agent.redact.redact_known_secret_values``) masks the raw values from
the per-home applied-secrets snapshot before they reach the provider-bound
message list.

Seeding mirrors ``tests/agent/test_secret_scope.py``: the per-home applied
snapshot (``hermes_cli.env_loader._SECRET_SOURCE_VALUES_BY_HOME``) is
populated directly and restored by monkeypatch.
"""

import pytest

from agent.redact import redact_sensitive_text, redact_terminal_output
from agent.tool_dispatch_helpers import make_tool_result_message


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    """Ensure redaction is active regardless of host HERMES_REDACT_SECRETS."""
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


@pytest.fixture()
def applied_secret_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a tmp home seeded with one arbitrary-named secret.

    Returns ``(home, name, value)``. The value is deliberately opaque —
    no vendor prefix, no secret keyword in the name — so only the
    exact-value pass can mask it.
    """
    from hermes_cli import env_loader

    name = "DATABASE_URL"
    value = "opaque-applied-db-value-9f2a"
    home_key = str(tmp_path.resolve())
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setitem(
        env_loader._SECRET_SOURCE_VALUES_BY_HOME, home_key, {name: value}
    )
    return tmp_path, name, value


class TestProviderEgressWire:
    """The primary wire: tool results appended to provider-bound messages."""

    def test_tool_result_message_masks_applied_secret(self, applied_secret_home):
        """A tool echoing the secret must not reach the provider verbatim."""
        _home, name, value = applied_secret_home
        echoed = f"$ printenv\n{name}={value}\nPATH=/usr/bin"
        msg = make_tool_result_message("terminal", echoed, "call_abc123")

        content = msg["content"]
        assert value not in content
        assert "***" in content
        assert f"{name}=" in content  # key survives; value masked

        # The provider-bound message list receives the masked content.
        provider_bound = [
            {"role": "assistant", "content": "run printenv", "tool_calls": []},
            msg,
        ]
        assert value not in str(provider_bound)

    def test_tool_result_message_multimodal_masks_applied_secret(
        self, applied_secret_home
    ):
        """Text-type parts of multimodal content are masked on the wire."""
        _home, name, value = applied_secret_home
        msg = make_tool_result_message(
            "browser_snapshot",
            [
                {"type": "text", "text": f"config read: {name}={value}"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
            "call_xyz",
        )
        parts = msg["content"]
        assert value not in parts[0]["text"]
        assert "***" in parts[0]["text"]

    def test_printenv_echo_masked_in_terminal_output(self, applied_secret_home):
        """terminal_tool / process_registry redact_terminal_output path."""
        _home, name, value = applied_secret_home
        out = redact_terminal_output(f"{name}={value}\nPATH=/usr/bin", "printenv")
        assert value not in out
        assert "PATH=/usr/bin" in out

    def test_file_read_echo_masked(self, applied_secret_home):
        """read_file/search_files/cat (file_read=True) mask applied secrets."""
        _home, name, value = applied_secret_home
        out = redact_sensitive_text(
            f"# .env\n{name}={value}", file_read=True, force=True
        )
        assert value not in out
        assert f"{name}=" in out

    def test_unrelated_text_unchanged_when_secret_not_echoed(
        self, applied_secret_home
    ):
        """Output that does not echo the secret passes through untouched."""
        _home, _name, value = applied_secret_home
        text = "ordinary build output, nothing sensitive here"
        assert make_tool_result_message("terminal", text, "call_1")["content"] == text
