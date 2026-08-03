"""Tests for tools.environments.local.build_subprocess_env — the single
factory for child-process environments (profile-home + secret-scrub owner).
"""

import os
import subprocess
import sys

import pytest

from tools.env_passthrough import clear_env_passthrough, register_env_passthrough
from tools.environments.local import build_subprocess_env, hermes_subprocess_env


# ---------------------------------------------------------------------------
# Unit: scrub path delegates to _sanitize_subprocess_env semantics
# ---------------------------------------------------------------------------

def test_scrub_on_strips_provider_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    env = build_subprocess_env()
    assert "ANTHROPIC_API_KEY" not in env


def test_scrub_on_strips_dynamic_internal_secret(monkeypatch):
    monkeypatch.setenv("AUXILIARY_VISION_API_KEY", "sk-aux")
    monkeypatch.setenv("GATEWAY_RELAY_FOO_TOKEN", "tok")
    env = build_subprocess_env()
    assert "AUXILIARY_VISION_API_KEY" not in env
    assert "GATEWAY_RELAY_FOO_TOKEN" not in env


def test_scrub_on_forwards_extra_like_sanitize_extra_env(monkeypatch):
    env = build_subprocess_env(extra={"MY_HARMLESS_VAR": "1"})
    assert env.get("MY_HARMLESS_VAR") == "1"
    # extra still goes through the blocklist on the scrub path
    env2 = build_subprocess_env(extra={"ANTHROPIC_API_KEY": "sk"})
    assert "ANTHROPIC_API_KEY" not in env2


# ---------------------------------------------------------------------------
# Unit: no-scrub path preserves content exactly
# ---------------------------------------------------------------------------


def test_no_scrub_inherit_profile_home_bridges_context_override(tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    token = set_hermes_home_override(str(tmp_path))
    try:
        env = build_subprocess_env(
            {"PATH": "/bin"}, scrub_secrets=False, inherit_profile_home=True
        )
    finally:
        reset_hermes_home_override(token)
    assert env["HERMES_HOME"] == str(tmp_path)


# ---------------------------------------------------------------------------
# E2E: real subprocess sees the factory's contract
# ---------------------------------------------------------------------------

def test_e2e_child_sees_hermes_home_and_no_planted_secret(tmp_path, monkeypatch):
    """A real child spawned with a factory-built env must see HERMES_HOME
    propagated and (with scrub on) a planted provider-style key absent."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-FAKE-planted")
    monkeypatch.setenv("AUXILIARY_FAKE_API_KEY", "sk-FAKE-aux")

    env = build_subprocess_env()  # scrub on (default)

    code = (
        "import os, json; "
        "print(json.dumps({'home': os.environ.get('HERMES_HOME'), "
        "'k1': 'ANTHROPIC_API_KEY' in os.environ, "
        "'k2': 'AUXILIARY_FAKE_API_KEY' in os.environ}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=env, capture_output=True, text=True, timeout=60, check=True,
    )
    import json

    result = json.loads(out.stdout)
    assert result["home"] == str(hermes_home)
    assert result["k1"] is False
    assert result["k2"] is False


def test_e2e_no_scrub_child_keeps_planted_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-FAKE-planted")
    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False)
    out = subprocess.run(
        [sys.executable, "-c",
         "import os; print(os.environ.get('ANTHROPIC_API_KEY', ''))"],
        env=env, capture_output=True, text=True, timeout=60, check=True,
    )
    assert out.stdout.strip() == "sk-FAKE-planted"


# ---------------------------------------------------------------------------
# Provenance-aware scrub: externally-applied secrets under ANY name shape
# (issue #77164)
#
# The shape predicates (_is_hermes_internal_secret + the static blocklist)
# only match credential-shaped names. Values applied from external secret
# sources (Bitwarden / 1Password / secret-source command) under
# non-credential-shaped names — DATABASE_URL, FOO, arbitrary 1Password item
# keys — were inherited verbatim by every spawned child. These tests pin the
# fix: the scrub consults hermes_cli.env_loader's per-home applied-secrets
# snapshot (get_secret_source_values), which is authoritative about WHAT came
# from an external source regardless of name shape.
# ---------------------------------------------------------------------------


@pytest.fixture
def applied_secrets(tmp_path, monkeypatch):
    """Simulate external secret-source application for a temp HERMES_HOME.

    Mirrors exactly what load_hermes_dotenv()/_hydrate_profile_secret_sources()
    record in ``hermes_cli.env_loader._SECRET_SOURCE_VALUES_BY_HOME`` for
    externally-applied secrets — including under NON-credential-shaped names
    that no shape predicate matches.
    """
    from hermes_cli import env_loader

    home = tmp_path / "hermes-home"
    home.mkdir()
    home_key = str(home.resolve())
    monkeypatch.setenv("HERMES_HOME", str(home))
    snapshot = {
        "DATABASE_URL": "postgres://user:supersecret@db",
        "FOO": "bar-value",
        # A credential-shaped key supplied by an external source: provenance
        # (not shape) must drive the strip.
        "OPENAI_API_KEY": "sk-applied-via-bitwarden",
    }
    env_loader._SECRET_SOURCE_VALUES_BY_HOME[home_key] = dict(snapshot)
    try:
        yield home_key, snapshot
    finally:
        env_loader._SECRET_SOURCE_VALUES_BY_HOME.pop(home_key, None)


def test_scrub_strips_applied_secret_names(applied_secrets, monkeypatch):
    """Non-credential-shaped applied secrets must be stripped from the
    terminal factory's env even though no shape predicate matches them."""
    monkeypatch.setenv("DATABASE_URL", "postgres://user:supersecret@db")
    monkeypatch.setenv("FOO", "bar-value")
    monkeypatch.setenv("MY_OWN_THING", "keep-me")  # .env/shell var, NOT applied
    env = build_subprocess_env()
    assert "DATABASE_URL" not in env
    assert "FOO" not in env
    # A user's own env (never applied from an external source) still flows.
    assert env["MY_OWN_THING"] == "keep-me"


def test_e2e_child_does_not_see_applied_secrets(applied_secrets, monkeypatch):
    """A real child spawned with the terminal factory's env must not see
    externally-applied secrets under non-credential-shaped names, while still
    seeing the user's own non-applied env vars."""
    monkeypatch.setenv("DATABASE_URL", "postgres://user:supersecret@db")
    monkeypatch.setenv("FOO", "bar-value")
    monkeypatch.setenv("MY_OWN_THING", "keep-me")

    env = build_subprocess_env()

    code = (
        "import os, json; "
        "print(json.dumps({'db': 'DATABASE_URL' in os.environ, "
        "'foo': 'FOO' in os.environ, "
        "'mine': os.environ.get('MY_OWN_THING')}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=env, capture_output=True, text=True, timeout=60, check=True,
    )
    import json

    result = json.loads(out.stdout)
    assert result["db"] is False
    assert result["foo"] is False
    assert result["mine"] == "keep-me"


def test_passthrough_applied_secret_survives_terminal_path(applied_secrets, monkeypatch):
    """env_passthrough wins on the terminal path: an explicitly registered
    applied name still reaches the child; a non-registered applied name is
    still stripped."""
    monkeypatch.setenv("DATABASE_URL", "postgres://user:supersecret@db")
    monkeypatch.setenv("FOO", "bar-value")
    try:
        register_env_passthrough(["DATABASE_URL"])
        env = build_subprocess_env()
        assert env.get("DATABASE_URL") == "postgres://user:supersecret@db"
        assert "FOO" not in env
    finally:
        clear_env_passthrough()


def test_hermes_subprocess_env_strips_applied_secrets(applied_secrets, monkeypatch):
    """Non-terminal surface: applied-secret names are stripped unconditionally
    (no passthrough concept there), and non-applied user env still flows."""
    monkeypatch.setenv("DATABASE_URL", "postgres://user:supersecret@db")
    monkeypatch.setenv("FOO", "bar-value")
    monkeypatch.setenv("MY_OWN_THING", "keep-me")
    env = hermes_subprocess_env()
    assert "DATABASE_URL" not in env
    assert "FOO" not in env
    assert env["MY_OWN_THING"] == "keep-me"


def test_hermes_subprocess_env_strips_applied_secrets_when_inheriting(applied_secrets, monkeypatch):
    """Applied-secret names are stripped even with inherit_credentials=True
    (same unconditional class as the static Tier-1 blocklist), while
    non-applied provider keys still flow on the inherit path."""
    monkeypatch.setenv("DATABASE_URL", "postgres://user:supersecret@db")
    monkeypatch.setenv("FOO", "bar-value")
    # Credential-shaped AND in the applied snapshot: provenance wins.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-applied-via-bitwarden")
    # Credential-shaped but user's own .env/shell, never applied: still flows.
    monkeypatch.setenv("GEMINI_API_KEY", "sk-own-env")
    env = hermes_subprocess_env(inherit_credentials=True)
    assert "DATABASE_URL" not in env
    assert "FOO" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["GEMINI_API_KEY"] == "sk-own-env"
