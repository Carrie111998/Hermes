"""obs.otel_tracing._load_env_once must honor HERMES_HOME.

Regression for the cron profile-plugin-tools failure class: ``_load_env_once``
hardcoded ``Path.home() / ".hermes" / ".env"``, so any process whose real
``~/.hermes/.env`` carries credentials (e.g. FIRECRAWL_API_KEY) had them
re-injected into ``os.environ`` at ``cron.scheduler`` import time — inside
pytest, *after* the autouse hermetic-environment fixture had already blanked
them. Downstream, ``check_web_api_key()`` flipped True on ambient host
credentials and tool-visibility tests passed/failed depending on the
developer's machine instead of the code.

The fix routes the lookup through the same resolution order as
:func:`hermes_constants.get_hermes_home` (context-local override →
``HERMES_HOME`` env var → platform default) so tests redirecting
``HERMES_HOME`` to an empty tempdir can never see host secrets.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """A throwaway HERMES_HOME with no .env, plus a poisoned real-home .env."""
    home = tmp_path / "hermes-root"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Simulate the developer's real ~/.hermes/.env existing and carrying a
    # credential. _load_env_once must NOT read it while HERMES_HOME points
    # elsewhere.
    fake_real_env = tmp_path / "fake-real-dotenv"
    fake_real_env.write_text("FIRECRAWL_API_KEY=fc-leaked-from-real-home\n", encoding="utf-8")
    return home, fake_real_env


def test_load_env_once_ignores_real_home_dotenv_when_hermes_home_redirected(
    isolated_home, monkeypatch
):
    from obs import otel_tracing

    home, _fake = isolated_home
    # The function's fallback target (Path.home()/.hermes/.env) would resolve
    # into the developer's live tree; point it at our poisoned file instead so
    # the old implementation provably leaks and the new one provably doesn't.
    monkeypatch.setattr(
        Path, "home", lambda: home.parent, raising=True
    )
    (home.parent / ".hermes").mkdir(exist_ok=True)
    (home.parent / ".hermes" / ".env").write_text(
        "FIRECRAWL_API_KEY=fc-poisoned\n", encoding="utf-8"
    )

    otel_tracing._load_env_once()

    assert "FIRECRAWL_API_KEY" not in os.environ


def test_load_env_once_reads_hermes_home_dotenv(
    isolated_home, monkeypatch
):
    from obs import otel_tracing

    home, _fake = isolated_home
    (home / ".env").write_text(
        "FIRECRAWL_API_KEY=fc-from-hermes-home\n", encoding="utf-8"
    )

    otel_tracing._load_env_once()

    assert os.environ.get("FIRECRAWL_API_KEY") == "fc-from-hermes-home"


def test_load_env_once_does_not_clobber_existing_environ_values(
    isolated_home, monkeypatch
):
    from obs import otel_tracing

    home, _fake = isolated_home
    (home / ".env").write_text(
        "FIRECRAWL_API_KEY=fc-from-file\n", encoding="utf-8"
    )
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-from-shell")

    otel_tracing._load_env_once()

    assert os.environ.get("FIRECRAWL_API_KEY") == "fc-from-shell"


def test_ensure_initialized_does_not_leak_host_credentials_via_get_tracer(
    isolated_home,
):
    """End-to-end: cron.scheduler's import-time get_tracer call must not
    re-inject host .env credentials after a fixture sanitized them."""
    from obs import otel_tracing

    # Force a fresh init path; ensure_initialized is idempotent per-process,
    # but a prior test in this session may have flipped _INITIALIZED.
    otel_tracing._INITIALIZED = False
    try:
        otel_tracing.ensure_initialized(service_name="test-no-leak")
    finally:
        otel_tracing._INITIALIZED = True

    assert "FIRECRAWL_API_KEY" not in os.environ
