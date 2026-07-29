"""The credential pool's detection layer must respect what's already in
auth.json. An entry with `source: env:WHATEVER_VAR` is valid even when
`access_token` is empty on disk, because the value actually lives at
the env var named in the source field.

This is the runtime-side counterpart to the seeding fix. The pool's
selection filter and runtime_api_key both need to consult the env at
request time, not require the value to be pre-populated in auth.json.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _write_env_file(home: Path, **env_vars):
    """Write a .env file under HERMES_HOME."""
    home.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in env_vars.items()]
    (home / ".env").write_text("\n".join(lines) + "\n")


def _make_env_pool(provider="opencode-go"):
    """Build a CredentialPool with one env-source entry whose access_token
    is empty (the on-disk state after persistence strips borrowed creds)."""
    from agent.credential_pool import CredentialPool, PooledCredential

    entry = PooledCredential(
        provider=provider,
        id="sec1234",
        label="secondary",
        auth_type="api_key",
        priority=1,
        source="env:OPENCODE_GO_API_KEY2",
        access_token="",
    )
    return CredentialPool(provider, [entry])


@pytest.fixture
def isolated_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
        "ZAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN", "OPENCODE_GO_API_KEY",
        "OPENCODE_GO_API_KEY2", "OPENAI_BASE_URL",
    ]:
        monkeypatch.delenv(key, raising=False)
    # load_env() in hermes_cli.config memoises on (path, mtime, size). The
    # cache can survive across tests if the same path is reused; clear it.
    try:
        from hermes_cli.config import invalidate_env_cache
        invalidate_env_cache()
    except (ImportError, AttributeError):
        pass
    return home


class TestPoolRespectsEnvSourceEntries:
    """Detection layer must honor source: env:* entries even when empty."""

    def test_env_source_entry_with_env_var_set_is_available(
        self, isolated_hermes_home
    ):
        """An env-source entry whose env var is set must be considered
        available for rotation, even though access_token is empty."""
        from agent.credential_pool import STATUS_OK

        _write_env_file(
            isolated_hermes_home, OPENCODE_GO_API_KEY2="backup-key"
        )
        pool = _make_env_pool()

        available = pool._available_entries()
        assert len(available) == 1
        assert available[0].source == "env:OPENCODE_GO_API_KEY2"

    def test_env_source_entry_with_env_var_unset_is_filtered(
        self, isolated_hermes_home
    ):
        """If the env var is also unset, the entry is genuinely empty and
        must be filtered out of rotation."""
        pool = _make_env_pool()
        # No env vars set

        available = pool._available_entries()
        assert available == []

    def test_manual_source_entry_with_empty_token_is_filtered(
        self, isolated_hermes_home
    ):
        """A manual entry with no token stays filtered. Only env-source
        entries get the env-var fallback."""
        from agent.credential_pool import CredentialPool, PooledCredential

        manual = PooledCredential(
            provider="opencode-go",
            id="man",
            label="manual",
            auth_type="api_key",
            priority=0,
            source="manual",
            access_token="",
        )
        pool = CredentialPool("opencode-go", [manual])

        available = pool._available_entries()
        assert available == []

    def test_runtime_api_key_resolves_env_source_from_secret(
        self, isolated_hermes_home
    ):
        """runtime_api_key must fetch the value from the env-named secret
        for env-source entries with empty access_token."""
        from agent.secret_scope import set_secret_scope, _SECRET_SCOPE
        from agent.credential_pool import _get_secret

        _write_env_file(
            isolated_hermes_home, OPENCODE_GO_API_KEY2="backup-key"
        )
        pool = _make_env_pool()
        entry = pool.entries()[0]

        # The value should come from the env file (or the active secret scope)
        assert entry.runtime_api_key == "backup-key"

    def test_runtime_api_key_falls_back_to_os_environ(
        self, isolated_hermes_home
    ):
        """If the .env file has no value but os.environ does, use that."""
        # No .env entry, but set the env var directly
        os.environ["OPENCODE_GO_API_KEY2"] = "os-environ-key"

        pool = _make_env_pool()
        entry = pool.entries()[0]
        assert entry.runtime_api_key == "os-environ-key"

    def test_runtime_api_key_empty_when_no_env_value(self, isolated_hermes_home):
        """If neither .env nor os.environ has the value, runtime_api_key
        is empty (entry is genuinely not configured)."""
        pool = _make_env_pool()
        entry = pool.entries()[0]
        assert entry.runtime_api_key == ""

    def test_runtime_api_key_prefers_persisted_token(self, isolated_hermes_home):
        """If access_token is populated on disk, the persisted value wins
        over the env var. The env fallback is only for borrowed creds."""
        from agent.credential_pool import PooledCredential

        _write_env_file(
            isolated_hermes_home, OPENCODE_GO_API_KEY2="env-value"
        )
        e = PooledCredential(
            provider="opencode-go",
            id="prim",
            label="primary",
            auth_type="api_key",
            priority=0,
            source="env:OPENCODE_GO_API_KEY2",
            access_token="persisted-key",
        )
        from agent.credential_pool import CredentialPool
        pool = CredentialPool("opencode-go", [e])
        assert pool.entries()[0].runtime_api_key == "persisted-key"
