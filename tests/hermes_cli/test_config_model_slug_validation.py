"""Tests for write-time model-slug validation (#97656).

``set_config_value`` now consults the target provider's cached catalog (the same
machinery the model picker uses) when a model-routing leaf key
(``model.default``, ``delegation.model``, ``auxiliary.<task>.model``) is
written. These tests assert the *behavior contract* — warn / confirm / fail-open
— rather than freezing catalog contents, and every network touch is mocked.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.config import set_config_value


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path):
    """Point HERMES_HOME at a temp dir so tests never touch real config."""
    env_file = tmp_path / ".env"
    env_file.touch()
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        yield tmp_path


def _read_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    return config_path.read_text() if config_path.exists() else ""


def _seed_provider(provider_key, provider_value, *_ignored):
    """Seed a runtime provider via the real set path (writes config.yaml)."""
    set_config_value(provider_key, provider_value)


_OPENROUTER_CATALOG = [
    "upstage/solar-pro4",
    "google/gemini-3-flash",
    "anthropic/claude-sonnet-4",
]


def _echoing_input(answer: str):
    """A builtins.input stand-in that echoes the prompt to stdout, like the
    real ``input()`` does, then returns the canned answer."""
    def _input(prompt=""):
        sys.stdout.write(str(prompt))
        sys.stdout.flush()
        return answer
    return _input


def _all_output(capsys):
    """Combined stdout+stderr for 'must not warn' assertions."""
    captured = capsys.readouterr()
    return captured.out + captured.err


# ---------------------------------------------------------------------------
# Known slug → no warning, write proceeds
# ---------------------------------------------------------------------------

class TestKnownSlug:
    def test_delegation_model_known_slug_no_warning(self, _isolated_hermes_home, capsys):
        _seed_provider("delegation.provider", "openrouter", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("delegation.model", "upstage/solar-pro4")
        assert "not in provider" not in _all_output(capsys)
        assert "upstage/solar-pro4" in _read_config(_isolated_hermes_home)

    def test_model_default_known_slug_no_warning(self, _isolated_hermes_home, capsys):
        _seed_provider("model.provider", "openrouter", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("model.default", "google/gemini-3-flash")
        assert "not in provider" not in _all_output(capsys)
        assert "google/gemini-3-flash" in _read_config(_isolated_hermes_home)

    def test_auxiliary_known_slug_no_warning(self, _isolated_hermes_home, capsys):
        _seed_provider("auxiliary.vision.provider", "openrouter", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("auxiliary.vision.model", "anthropic/claude-sonnet-4")
        assert "not in provider" not in _all_output(capsys)
        assert "anthropic/claude-sonnet-4" in _read_config(_isolated_hermes_home)

    def test_non_model_key_never_validates(self, _isolated_hermes_home, capsys):
        # A non-model-routing key must never consult the catalog.
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            side_effect=AssertionError("catalog consulted for non-model key"),
        ):
            set_config_value("delegation.provider", "openrouter")
        assert "not in provider" not in _all_output(capsys)


# ---------------------------------------------------------------------------
# Unknown slug + interactive TTY → warning + abort on N
# ---------------------------------------------------------------------------

class TestUnknownSlugInteractive:
    def test_unknown_slug_tty_abort_on_n(
        self, _isolated_hermes_home, capsys, monkeypatch
    ):
        _seed_provider("delegation.provider", "openrouter", _isolated_hermes_home)
        monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            with patch("builtins.input", _echoing_input("N")):
                with pytest.raises(SystemExit) as exc:
                    set_config_value("delegation.model", "totally-fake-model-xyz")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "not in provider" in captured.err  # warning → stderr
        assert "Set anyway? [y/N]" in captured.out  # prompt → stdout
        # Value must NOT be persisted.
        assert "totally-fake-model-xyz" not in _read_config(_isolated_hermes_home)

    def test_unknown_slug_tty_confirm_yes_writes(
        self, _isolated_hermes_home, capsys, monkeypatch
    ):
        _seed_provider("delegation.provider", "openrouter", _isolated_hermes_home)
        monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            with patch("builtins.input", _echoing_input("y")):
                set_config_value("delegation.model", "totally-fake-model-xyz")
        assert "not in provider" in capsys.readouterr().err
        assert "totally-fake-model-xyz" in _read_config(_isolated_hermes_home)


# ---------------------------------------------------------------------------
# Unknown slug non-TTY → warning + write proceeds (fail-open)
# ---------------------------------------------------------------------------

class TestUnknownSlugNonTty:
    def test_unknown_slug_nontty_warns_and_proceeds(
        self, _isolated_hermes_home, capsys, monkeypatch
    ):
        _seed_provider("delegation.provider", "openrouter", _isolated_hermes_home)
        monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("delegation.model", "totally-fake-model-xyz")
        assert "not in provider" in capsys.readouterr().err
        assert "totally-fake-model-xyz" in _read_config(_isolated_hermes_home)

    def test_unknown_slug_force_skips_prompt_but_warns(
        self, _isolated_hermes_home, capsys, monkeypatch
    ):
        _seed_provider("delegation.provider", "openrouter", _isolated_hermes_home)
        monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            # --force must skip the prompt but still print the warning.
            with patch(
                "builtins.input",
                side_effect=AssertionError("prompt shown despite --force"),
            ):
                set_config_value("delegation.model", "totally-fake-model-xyz", force=True)
        captured = capsys.readouterr()
        assert "not in provider" in captured.err
        assert "Set anyway?" not in captured.out
        assert "totally-fake-model-xyz" in _read_config(_isolated_hermes_home)


# ---------------------------------------------------------------------------
# Custom provider → no validation
# ---------------------------------------------------------------------------

class TestCustomProvider:
    def test_custom_provider_skips_validation(self, _isolated_hermes_home, capsys):
        _seed_provider("delegation.provider", "custom", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            side_effect=AssertionError("catalog consulted for custom provider"),
        ):
            set_config_value("delegation.model", "my-custom-model")
        assert "not in provider" not in _all_output(capsys)
        assert "my-custom-model" in _read_config(_isolated_hermes_home)

    def test_empty_provider_skips_validation(self, _isolated_hermes_home, capsys):
        # delegation.provider defaults to '' (inherit parent) — skip silently.
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            side_effect=AssertionError("catalog consulted for empty provider"),
        ):
            set_config_value("delegation.model", "inherited-model")
        assert "not in provider" not in _all_output(capsys)
        assert "inherited-model" in _read_config(_isolated_hermes_home)


# ---------------------------------------------------------------------------
# Validation exception → write succeeds (fail-open)
# ---------------------------------------------------------------------------

class TestValidationFailsOpen:
    def test_network_error_fails_open(self, _isolated_hermes_home, capsys):
        _seed_provider("delegation.provider", "openrouter", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            side_effect=RuntimeError("catalog fetch failed"),
        ):
            # Must not raise and must still write — validation never blocks set.
            set_config_value("delegation.model", "totally-fake-model-xyz")
        assert "not in provider" not in _all_output(capsys)
        assert "totally-fake-model-xyz" in _read_config(_isolated_hermes_home)

    def test_empty_catalog_fails_open(self, _isolated_hermes_home, capsys):
        _seed_provider("delegation.provider", "openrouter", _isolated_hermes_home)
        with patch("hermes_cli.models.cached_provider_model_ids", return_value=[]):
            set_config_value("delegation.model", "totally-fake-model-xyz")
        assert "not in provider" not in _all_output(capsys)
        assert "totally-fake-model-xyz" in _read_config(_isolated_hermes_home)
