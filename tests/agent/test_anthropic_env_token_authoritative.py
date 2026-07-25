"""HERMES_ANTHROPIC_TOKEN_AUTHORITATIVE pins an operator-managed ANTHROPIC_TOKEN.

Without the flag, ``resolve_anthropic_token()`` deliberately prefers Claude
Code's refreshable credential record over a static env OAuth token (so a
stale persisted setup token cannot block refresh). That heuristic backfires
when ANTHROPIC_TOKEN is *actively managed* by an external rotation script:
Claude Code's login can belong to a different — possibly rate-limited —
account, and the silent swap moves Hermes onto the wrong rate-limit pool
with no log line above DEBUG.

The opt-in flag declares "this env token is authoritative — never shadow it".
Default behavior (flag unset) is unchanged.
"""

import pytest

ENV_TOKEN = "sk-ant-oat01-managed-by-operator"
CLAUDE_CODE_TOKEN = "sk-ant-oat01-claude-code-borrowed"


def _claude_code_creds():
    return {
        "accessToken": CLAUDE_CODE_TOKEN,
        "refreshToken": "rt-refreshable",
        "expiresAt": 4102444800000,  # 2100-01-01 — always valid
        "source": "file",
    }


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "HERMES_ANTHROPIC_TOKEN_AUTHORITATIVE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials",
        _claude_code_creds,
    )
    return monkeypatch


def test_authoritative_env_token_is_never_shadowed(isolated_env):
    isolated_env.setenv("ANTHROPIC_TOKEN", ENV_TOKEN)
    isolated_env.setenv("HERMES_ANTHROPIC_TOKEN_AUTHORITATIVE", "1")

    from agent.anthropic_adapter import resolve_anthropic_token

    assert resolve_anthropic_token() == ENV_TOKEN


def test_default_still_prefers_refreshable_claude_code_creds(isolated_env):
    # Documents the existing default: without the opt-in flag, a refreshable
    # Claude Code credential is preferred over a static env OAuth token.
    isolated_env.setenv("ANTHROPIC_TOKEN", ENV_TOKEN)

    from agent.anthropic_adapter import resolve_anthropic_token

    assert resolve_anthropic_token() == CLAUDE_CODE_TOKEN


def test_flag_without_env_token_falls_through_to_claude_code(isolated_env):
    # The flag pins an EXPLICIT env token only; with no token set, the normal
    # resolution chain (Claude Code creds, pool, API key) is unchanged.
    isolated_env.setenv("HERMES_ANTHROPIC_TOKEN_AUTHORITATIVE", "1")

    from agent.anthropic_adapter import resolve_anthropic_token

    assert resolve_anthropic_token() == CLAUDE_CODE_TOKEN
