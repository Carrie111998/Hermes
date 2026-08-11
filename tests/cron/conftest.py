"""Cron-test fixtures.

Provides a default ``HERMES_MODEL`` for cron run_job tests so each one
doesn't have to spell out a model. The global conftest blanks
HERMES_MODEL hermetically; without this autouse fixture every cron test
that exercises ``run_job`` would hit the fail-fast guard added in
``cron/scheduler.py`` (see issue #23979) and have to be rewritten.

Tests that specifically need ``HERMES_MODEL`` unset — model-resolution
edge cases — call ``monkeypatch.delenv("HERMES_MODEL", raising=False)``
inside the test, which overrides this fixture's value for that scope.
"""

import pytest


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch):
    """Pin a default HERMES_MODEL so cron run_job tests have a resolvable model."""
    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    yield


@pytest.fixture(autouse=True)
def _no_live_host_probes(monkeypatch):
    """Keep ``run_job`` off the developer's machine and off the network.

    Building an agent turn reaches two pieces of production code that do
    LIVE I/O against the host. Neither is a production bug — both are
    deliberate, both fail open — but both are pure wall-clock inside a unit
    test, and together they pushed
    ``test_cron_run_job_codex_path_handles_internal_401_refresh`` to ~34s,
    past the 30s ``addopts`` cap. ``--timeout-method=thread`` responds by
    dumping thread tracebacks and hard-exiting the process, so
    ``pytest tests/events tests/cron`` never printed a summary line and the
    overrun read as a deadlock in ``concurrent.futures.wait``.

    1. ``tools.env_probe`` shells out to ``python3 --version``,
       ``python -m pip``, and a PEP 668 check to describe the local Python
       toolchain. On this Windows host ``python3`` is a Microsoft Store app
       alias, so the probe never finishes and
       ``get_environment_probe_line()`` burns its full
       ``_PROBE_WAIT_TIMEOUT`` (10s) before failing open with "". Seeding
       the same "" answer instantly is behaviour-identical and 10s cheaper.
       Measured: 9.60s of a single test's runtime.

    2. ``agent.model_metadata._fetch_codex_oauth_context_lengths`` issues an
       authenticated ``GET https://chatgpt.com/backend-api/codex/models`` to
       read real context windows for Codex OAuth slugs. A unit test must not
       send outbound traffic — least of all bearing a fake ``Bearer`` token —
       and its ``timeout=(5, 10)`` makes the cost network-dependent.
       Returning ``{}`` selects the hardcoded ``_CODEX_OAUTH_CONTEXT_FALLBACK``
       table, which is exactly what the live probe falls back to when it
       fails. Measured: 2.48s of a single test's runtime.

    Both are stubbed at their public entry points, so tests that exercise
    the probes themselves (``tests/tools/test_env_probe.py``,
    ``tests/run_agent/test_run_agent.py``) are untouched — they live outside
    ``tests/cron/`` and drive the internals directly.
    """
    from tools import env_probe

    monkeypatch.setattr(
        env_probe, "get_environment_probe_line", lambda **kwargs: ""
    )
    monkeypatch.setattr(
        env_probe, "warm_environment_probe_async", lambda *a, **k: None
    )

    from agent import model_metadata

    monkeypatch.setattr(
        model_metadata, "_fetch_codex_oauth_context_lengths", lambda token: {}
    )
    yield
