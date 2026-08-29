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
        monkeypatch.setattr("hermes_cli.config._is_interactive", lambda: True)
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
        monkeypatch.setattr("hermes_cli.config._is_interactive", lambda: True)
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
        monkeypatch.setattr("hermes_cli.config._is_interactive", lambda: False)
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
        monkeypatch.setattr("hermes_cli.config._is_interactive", lambda: True)
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
        # Both delegation.provider AND model.provider are '' (nothing to
        # inherit) — the parent fallback finds an empty provider, so validation
        # is skipped silently. No catalog may be consulted.
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


# ---------------------------------------------------------------------------
# model.default is a genuine model-routing leaf (validated via model.provider)
# ---------------------------------------------------------------------------

class TestModelDefault:
    def test_model_default_is_actually_validated(self, _isolated_hermes_home, capsys):
        # Regression: previously `_model_routing_provider_key('model.default')`
        # returned None (the leaf guard only matched `<...>.model`), so this
        # key was NEVER validated. It must consult model.provider's catalog.
        _seed_provider("model.provider", "openrouter", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("model.default", "totally-fake-model-xyz")
        assert "not in provider" in capsys.readouterr().err
        assert "totally-fake-model-xyz" in _read_config(_isolated_hermes_home)

    def test_model_default_known_slug_validated_no_warning(
        self, _isolated_hermes_home, capsys
    ):
        # A KNOWN slug must not only avoid the warning but must go THROUGH the
        # catalog check (it consulted model.provider) rather than short-circuit.
        _seed_provider("model.provider", "openrouter", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("model.default", "google/gemini-3-flash")
        assert "not in provider" not in _all_output(capsys)
        assert "google/gemini-3-flash" in _read_config(_isolated_hermes_home)


# ---------------------------------------------------------------------------
# Empty delegation/auxiliary provider inherits the parent model.provider
# ---------------------------------------------------------------------------

class TestProviderInheritance:
    def test_delegation_model_inherits_parent_provider(
        self, _isolated_hermes_home, capsys
    ):
        # delegation.provider is '' (inherit parent); model.provider is set.
        # The model must be validated against the PARENT (model.provider).
        _seed_provider("model.provider", "openrouter", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("delegation.model", "totally-not-a-model")
        assert "not in provider" in capsys.readouterr().err
        assert "totally-not-a-model" in _read_config(_isolated_hermes_home)

    def test_auxiliary_model_inherits_parent_provider(
        self, _isolated_hermes_home, capsys
    ):
        # auxiliary.vision.provider explicitly '' (inherit parent); the model
        # must be validated against that inheritable parent (model.provider).
        _seed_provider("model.provider", "openrouter", _isolated_hermes_home)
        _seed_provider("auxiliary.vision.provider", "", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("auxiliary.vision.model", "totally-not-a-model")
        assert "not in provider" in capsys.readouterr().err
        assert "totally-not-a-model" in _read_config(_isolated_hermes_home)

    def test_inherited_provider_matches_catalog_no_warning(
        self, _isolated_hermes_home, capsys
    ):
        _seed_provider("model.provider", "openrouter", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("delegation.model", "upstage/solar-pro4")
        assert "not in provider" not in _all_output(capsys)
        assert "upstage/solar-pro4" in _read_config(_isolated_hermes_home)


# ---------------------------------------------------------------------------
# Setting a provider re-validates an already-written sibling model (#97656 gap)
# ---------------------------------------------------------------------------

class TestProviderSetFollowup:
    def test_set_provider_revalidates_sibling_model(
        self, _isolated_hermes_home, capsys
    ):
        # Model written first while provider was empty (validation legitimately
        # skipped) — then provider is set. The sibling must be re-checked and
        # warn (never block) against the now-known provider.
        set_config_value("delegation.model", "totally-fake-model-xyz")
        assert "not in provider" not in _all_output(capsys)  # skipped by empty provider
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("delegation.provider", "openrouter")
        assert "not in provider" in capsys.readouterr().err
        # Provider write still succeeds (fail-open, warn-only).
        assert "openrouter" in _read_config(_isolated_hermes_home)

    def test_model_provider_set_revalidates_model_default(
        self, _isolated_hermes_home, capsys
    ):
        set_config_value("model.default", "totally-fake-model-xyz")
        assert "not in provider" not in _all_output(capsys)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("model.provider", "openrouter")
        assert "not in provider" in capsys.readouterr().err
        assert "openrouter" in _read_config(_isolated_hermes_home)


# ---------------------------------------------------------------------------
# Interactive detection requires BOTH stdin and stdout to be TTYs
# ---------------------------------------------------------------------------

class TestIsInteractive:
    def test_requires_both_stdin_and_stdout_tty(self, monkeypatch):
        from hermes_cli.config import _is_interactive

        monkeypatch.setattr("hermes_cli.config.sys.stdin", SimpleNamespace(isatty=lambda: True))
        # stdout redirected (agent pipe / capture) → not interactive.
        monkeypatch.setattr("hermes_cli.config.sys.stdout", SimpleNamespace(isatty=lambda: False))
        assert _is_interactive() is False
        # both TTYs → interactive.
        monkeypatch.setattr("hermes_cli.config.sys.stdout", SimpleNamespace(isatty=lambda: True))
        assert _is_interactive() is True


# ---------------------------------------------------------------------------
# OpenRouter ':variant' broadening only applies to the OpenRouter provider
# ---------------------------------------------------------------------------

class TestVariantStripping:
    def test_openrouter_variant_matches_base_in_catalog(
        self, _isolated_hermes_home, capsys
    ):
        _seed_provider("delegation.provider", "openrouter", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=_OPENROUTER_CATALOG,
        ):
            set_config_value("delegation.model", "upstage/solar-pro4:nitro")
        # ':nitro' is an OpenRouter base-id variant → no warning.
        assert "not in provider" not in _all_output(capsys)
        assert "upstage/solar-pro4:nitro" in _read_config(_isolated_hermes_home)

    def test_variant_not_broadened_for_other_provider(
        self, _isolated_hermes_home, capsys
    ):
        # A ':variant' slug for a NON-OpenRouter provider keeps the exact-match
        # result — the base-id broadening is OpenRouter-specific.
        _seed_provider("delegation.provider", "anthropic", _isolated_hermes_home)
        with patch(
            "hermes_cli.models.cached_provider_model_ids",
            return_value=["upstage/solar-pro4", "anthropic/claude-sonnet-4"],
        ):
            set_config_value("delegation.model", "upstage/solar-pro4:nitro")
        assert "not in provider" in capsys.readouterr().err
        assert "upstage/solar-pro4:nitro" in _read_config(_isolated_hermes_home)
