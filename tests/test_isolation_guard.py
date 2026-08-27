"""Contract test verifying test suite isolation and import-time sandboxing in hermes-agent."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

_PROBE_ENV = "HERMES_IMPORT_GUARD_PROBE"
_SENTINEL_NAME = "HERMES-REAL-VAULT-SENTINEL"


def test_conftest_sandboxes_state_before_module_imports() -> None:
    """Verify that conftest.py redirects HERMES_HOME, HERMES_DOTFILES_DIR, and OBSIDIAN_VAULT at import time."""
    from tests.conftest import (
        HERMES_DOTFILES_DIR_AT_CONFTEST_IMPORT,
        HERMES_HOME_AT_CONFTEST_IMPORT,
        OBSIDIAN_VAULT_AT_CONFTEST_IMPORT,
        OBSIDIAN_VAULT_PATH_AT_CONFTEST_IMPORT,
    )

    real_home = (Path.home() / ".hermes").resolve()
    real_dotfiles = (Path.home() / "Dev" / "dotfiles").resolve()
    real_vault = (Path.home() / "Dev" / "obsidian-vault").resolve()

    assert HERMES_HOME_AT_CONFTEST_IMPORT, "conftest must set HERMES_HOME at import time"
    assert HERMES_DOTFILES_DIR_AT_CONFTEST_IMPORT, "conftest must set HERMES_DOTFILES_DIR at import time"
    assert OBSIDIAN_VAULT_AT_CONFTEST_IMPORT, "conftest must set OBSIDIAN_VAULT at import time"
    assert OBSIDIAN_VAULT_PATH_AT_CONFTEST_IMPORT, "conftest must set OBSIDIAN_VAULT_PATH at import time"

    assert Path(HERMES_HOME_AT_CONFTEST_IMPORT).resolve() != real_home
    assert Path(HERMES_DOTFILES_DIR_AT_CONFTEST_IMPORT).resolve() != real_dotfiles
    assert Path(OBSIDIAN_VAULT_AT_CONFTEST_IMPORT).resolve() != real_vault
    assert Path(OBSIDIAN_VAULT_PATH_AT_CONFTEST_IMPORT).resolve() != real_vault


def test_subprocess_credential_scrubbing_contract() -> None:
    """Spawn a clean Python subprocess with fake exported keys and verify scrubbing."""
    probe_code = f"""
import os
import sys

sys.path.insert(0, {repr(str(TESTS_DIR))})
from tests import conftest

# Verify credential detection
assert conftest._is_credential_var("TEST_SERVICE_API_KEY") is True
assert conftest._is_credential_var("AWS_SECRET_ACCESS_KEY") is True
assert conftest._is_credential_var("CLAUDE_CODE_OAUTH_TOKEN") is True
assert conftest._is_credential_var("SAFE_CONFIG_PATH") is False
assert conftest._looks_like_credential("LINEAR_API_KEY") is True
assert conftest._looks_like_credential("OPENAI_API_KEY") is True
print("GUARD_PROBE_PASSED")
"""
    env = dict(os.environ)
    env["TEST_SERVICE_API_KEY"] = "sentinel-key-12345"
    env["LINEAR_API_KEY"] = "sentinel-linear-key"

    result = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"Probe failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "GUARD_PROBE_PASSED" in result.stdout


def test_probe_child_binds_guarded_paths() -> None:
    """Child-only probe. Skipped in the parent run; driven by test_import_guard_beats_exported_env."""
    if not os.environ.get(_PROBE_ENV):
        pytest.skip("child-only probe, driven by test_import_guard_beats_exported_env")

    # The parent exported HERMES_HOME=<...>/HERMES-REAL-VAULT-SENTINEL before
    # spawning us. conftest must have overridden it at import.
    assert _SENTINEL_NAME not in os.environ["HERMES_HOME"]
    assert _SENTINEL_NAME not in os.environ["HERMES_DOTFILES_DIR"]
    assert _SENTINEL_NAME not in os.environ["OBSIDIAN_VAULT"]
    assert _SENTINEL_NAME not in os.environ["OBSIDIAN_VAULT_PATH"]

    # Credential env vars must be stripped by conftest at import time.
    assert "LINEAR_API_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "GEMINI_API_KEY" not in os.environ
    assert "SLACK_BOT_TOKEN" not in os.environ
    assert "GITHUB_TOKEN" not in os.environ
    assert "CUSTOM_TEST_API_KEY" not in os.environ
    assert "SOME_SERVICE_TOKEN" not in os.environ
    assert "DATABASE_PASSWORD" not in os.environ
    assert "APP_WEBHOOK_SECRET" not in os.environ
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ

    # Deterministic environment variables must be set.
    assert os.environ.get("TZ") == "UTC"
    assert os.environ.get("LANG") == "C.UTF-8"
    assert os.environ.get("LC_ALL") == "C.UTF-8"
    assert os.environ.get("PYTHONHASHSEED") == "0"
    assert os.environ.get("AWS_EC2_METADATA_DISABLED") == "true"


def test_import_guard_beats_exported_env(tmp_path: Path) -> None:
    """Export a sentinel vault root and credentials, then assert child ignored/stripped them."""
    sentinel = tmp_path / _SENTINEL_NAME
    sentinel.mkdir(parents=True)

    env = {
        **os.environ,
        "HERMES_HOME": str(sentinel / "hermes_home"),
        "HERMES_DOTFILES_DIR": str(sentinel / "dotfiles"),
        "OBSIDIAN_VAULT": str(sentinel / "vault"),
        "OBSIDIAN_VAULT_PATH": str(sentinel / "vault"),
        "LINEAR_API_KEY": "sentinel-linear-key",
        "OPENAI_API_KEY": "sentinel-openai-key",
        "ANTHROPIC_API_KEY": "sentinel-anthropic-key",
        "GEMINI_API_KEY": "sentinel-gemini-key",
        "SLACK_BOT_TOKEN": "sentinel-slack-token",
        "GITHUB_TOKEN": "sentinel-gh-token",
        "CUSTOM_TEST_API_KEY": "sentinel-custom-key",
        "SOME_SERVICE_TOKEN": "sentinel-token",
        "DATABASE_PASSWORD": "sentinel-password",
        "APP_WEBHOOK_SECRET": "sentinel-webhook-secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "sentinel-claude-oauth",
        "TZ": "America/New_York",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PYTHONHASHSEED": "12345",
        "AWS_EC2_METADATA_DISABLED": "false",
        _PROBE_ENV: "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{__file__}::test_probe_child_binds_guarded_paths",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "conftest's import-time guard did not override an exported "
        f"HERMES_HOME or scrub credential env vars.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
