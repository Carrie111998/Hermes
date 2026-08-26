"""The test suite must never write into the operator's real Hermes logs or dotfiles,
and credentials must be strictly isolated at import time.

`hermes_cli/main.py` calls `setup_logging()` at module scope, which resolves
`get_hermes_home()` and attaches rotating file handlers to the ROOT logger.
Importing it - which many test modules do, directly or transitively - wires
the whole pytest session's logging to `<HERMES_HOME>/logs/agent.log`.

Similarly, config saving and integrity watchdog scripts inspect `HERMES_DOTFILES_DIR`
and `HERMES_CONFIG`, which default to `~/Dev/dotfiles` and `~/.hermes/config.yaml`.
If unisolated, test runs silently write seal entries into the real `dotfiles/hermes/config_integrity.jsonl`.

`tests/conftest.py` sets HERMES_HOME, HERMES_DOTFILES_DIR, OBSIDIAN_VAULT, etc.
at module scope and strips credentials at import time for that reason.
"""

import logging
import os
from pathlib import Path

import pytest


def _real_hermes_home() -> Path:
    """Where the operator's logs live, ignoring any test sandboxing."""
    return Path.home() / ".hermes"


def _real_dotfiles_dir() -> Path:
    """Where the operator's dotfiles live, ignoring any test sandboxing."""
    return Path.home() / "Dev" / "dotfiles"


def _all_file_destinations() -> list[str]:
    """Every file path the root logger can reach, including via a QueueHandler.

    Logging is routed through a queue, so the file handlers hang off the
    listener rather than the root logger - checking `root.handlers` alone
    reports nothing and looks falsely clean.
    """
    seen: list[str] = []

    def collect(handlers) -> None:
        for handler in handlers or ():
            path = getattr(handler, "baseFilename", None)
            if path:
                seen.append(str(path))
            listener = getattr(handler, "listener", None)
            if listener is not None:
                collect(getattr(listener, "handlers", ()))

    collect(logging.getLogger().handlers)

    try:
        import hermes_logging

        listener = getattr(hermes_logging, "_queue_listener", None)
        if listener is not None:
            collect(getattr(listener, "handlers", ()))
    except Exception:
        pass

    return seen


class TestLogIsolation:
    def test_hermes_home_is_sandboxed_before_imports(self):
        # Deliberately NOT os.environ: by test time the per-test `_isolate_env`
        # fixture has sandboxed HERMES_HOME, so reading it here would pass even
        # with the conftest block deleted. Assert the value captured at conftest
        # import, which is the moment that actually matters.
        from tests.conftest import HERMES_HOME_AT_CONFTEST_IMPORT as home

        assert home, "conftest must set HERMES_HOME before test modules import"
        assert Path(home).resolve() != _real_hermes_home().resolve(), (
            f"HERMES_HOME pointed at the operator's real home ({home}) when "
            "conftest loaded; import-time setup_logging() writes to their agent.log"
        )

    def test_importing_the_cli_does_not_target_the_real_logs(self):
        pytest.importorskip("hermes_cli.main")

        real_logs = str(_real_hermes_home() / "logs")
        offenders = [p for p in _all_file_destinations() if p.startswith(real_logs)]

        assert offenders == [], (
            "the test session is writing into the operator's real Hermes logs:\n  "
            + "\n  ".join(offenders)
        )


class TestEnvironmentIsolation:
    def test_dotfiles_dir_is_sandboxed_before_imports(self):
        from tests.conftest import HERMES_DOTFILES_DIR_AT_CONFTEST_IMPORT as dotfiles

        assert dotfiles, "conftest must set HERMES_DOTFILES_DIR before test modules import"
        assert Path(dotfiles).resolve() != _real_dotfiles_dir().resolve(), (
            f"HERMES_DOTFILES_DIR pointed at operator's real dotfiles ({dotfiles}) when "
            "conftest loaded; tests calling seal.py or config_integrity write to real jsonl"
        )

    def test_obsidian_vault_is_sandboxed_before_imports(self):
        from tests.conftest import (
            OBSIDIAN_VAULT_AT_CONFTEST_IMPORT as vault,
            OBSIDIAN_VAULT_PATH_AT_CONFTEST_IMPORT as vault_path,
        )

        assert vault, "conftest must set OBSIDIAN_VAULT before test modules import"
        assert vault_path, "conftest must set OBSIDIAN_VAULT_PATH before test modules import"
        assert Path(vault).resolve() != (Path.home() / "Dev" / "obsidian-vault").resolve()
        assert Path(vault_path).resolve() != (Path.home() / "Dev" / "obsidian-vault").resolve()

    def test_kanban_and_config_paths_are_sandboxed_before_imports(self):
        from tests.conftest import (
            HERMES_BASE_HOME_AT_CONFTEST_IMPORT as base_home,
            HERMES_CONFIG_PATH_AT_CONFTEST_IMPORT as config_path,
            HERMES_KANBAN_HOME_AT_CONFTEST_IMPORT as kanban_home,
        )

        assert base_home, "conftest must set HERMES_BASE_HOME before test modules import"
        assert config_path, "conftest must set HERMES_CONFIG_PATH before test modules import"
        assert kanban_home, "conftest must set HERMES_KANBAN_HOME before test modules import"
        assert Path(base_home).resolve() != _real_hermes_home().resolve()
        assert Path(kanban_home).resolve() != _real_hermes_home().resolve()
        assert Path(config_path).resolve() != (_real_hermes_home() / "config.yaml").resolve()

    def test_looks_like_credential_detects_sensitive_keys(self):
        from tests.conftest import _looks_like_credential

        sensitive_keys = [
            "LINEAR_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "MISTRAL_API_KEY",
            "GROQ_API_KEY",
            "DEEPSEEK_API_KEY",
            "OPENROUTER_API_KEY",
            "HERMES_CONFIG_PASSWORD",
            "HERMES_CONFIG_PASSPHRASE",
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "CUSTOM_API_KEY",
            "MY_SERVICE_SECRET",
            "TEST_AUTH_TOKEN",
            "WEBHOOK_SECRET",
            "DB_PASSWORD",
        ]
        for key in sensitive_keys:
            assert _looks_like_credential(key), f"Key {key} should be recognized as a credential"

        harmless_keys = [
            "PATH",
            "PYTHONPATH",
            "LANG",
            "LC_ALL",
            "TZ",
            "HOME",
            "USER",
            "HERMES_HOME",
            "HERMES_DOTFILES_DIR",
            "OBSIDIAN_VAULT",
        ]
        for key in harmless_keys:
            assert not _looks_like_credential(key), f"Key {key} should not be flagged as a credential"

    def test_no_credentials_present_in_test_environment(self):
        from tests.conftest import _looks_like_credential

        leaked = [k for k in os.environ.keys() if _looks_like_credential(k)]
        assert leaked == [], f"Found unstripped credential env vars in test environment: {leaked}"

