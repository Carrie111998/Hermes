"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import sys

import pytest


@pytest.fixture
def restore_purged_modules():
    """Put ``sys.modules`` back the way a HERMES_HOME purge found it.

    The ``isolated_kanban_home*`` fixtures drop every ``hermes_cli*`` /
    ``hermes_state*`` / ``hermes_constants`` module and re-import, which is the
    right way to pick up a changed ``HERMES_HOME``. What they never did is put
    them back, so every test collected *after* those files ran against freshly
    imported duplicates while already-imported test modules still held
    references to the originals.

    pytest imports every test module during collection, up front, so
    ``tests/hermes_cli/test_tools_config.py`` binds ``tools_command`` from the
    original module object.  After the purge,
    ``monkeypatch.setattr("hermes_cli.tools_config._prompt_choice", fake)``
    resolves that string by *importing* — the name is gone, so Python builds a
    **fresh** module object and the patch lands on that one instead.  The
    already-bound ``tools_command`` still reads the original module's globals,
    so the patch silently does nothing and the real interactive/network code
    path runs.  That is what hung ``tests/hermes_cli`` at 83% on
    ``test_configure_all_platforms_configures_selected_tool_missing_provider``.

    Restoring ``sys.modules`` alone is NOT enough, and this is the part that is
    easy to get wrong: ``import a.b as x`` reads ``sys.modules``, while
    ``from a.b import y`` reads the attribute ``b`` on the parent package. Fix
    only the first and half the imports still resolve to the stale copy. Same
    two-step as ``tests/agent/test_verification_stop_caching.py``.
    """
    snapshot = dict(sys.modules)
    yield
    purged = {
        name: module for name, module in snapshot.items()
        if sys.modules.get(name) is not module
    }
    for name, module in purged.items():
        sys.modules[name] = module
    for name, module in purged.items():
        parent_name, _, child = name.rpartition(".")
        if not parent_name:
            continue
        parent = sys.modules.get(parent_name)
        if parent is not None and getattr(parent, child, None) is not module:
            setattr(parent, child, module)


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


@pytest.fixture(autouse=True)
def _stub_provider_model_fetch(request, monkeypatch):
    """Default ``cached_provider_model_ids`` to ``[]`` for every test.

    ``list_authenticated_providers`` (and the picker built on it) calls
    ``hermes_cli.models.cached_provider_model_ids`` for every credentialed
    provider. On a cache miss that is a LIVE path: an outbound model-catalog
    fetch per provider, followed by ``_save_provider_models_cache`` ->
    ``utils.atomic_json_write`` -> ``os.fsync`` against the developer's real
    provider-model cache file. Tests that only exercise the *credential* gate
    pay all of it -- measured 13-22s per test in
    ``test_authenticated_providers_exhausted_pool.py``.

    That matters beyond slowness. ``--timeout-method=thread`` (pyproject.toml)
    cannot raise into the main thread, so a test crossing the 30s cap takes the
    WHOLE pytest process down: no summary line, and the reported failure set is
    whatever had accumulated when the run died. Because these tests sit right
    at the cap, the set varied between invocations -- a standing trap for
    anyone taking a ``tests/hermes_cli`` baseline.

    ``[]`` is the semantically safe default, not merely a fast one: every
    caller already treats an empty result as "live discovery unavailable" and
    falls back to the curated static list (``curated.get(slug, [])``), which is
    exactly what happens offline. The credential gate under test is unaffected.

    Tests that assert on live discovery opt out with
    ``@pytest.mark.real_provider_model_fetch``. Tests needing specific model
    ids keep patching the same attribute themselves -- a per-test
    ``monkeypatch`` runs after this fixture and therefore still wins.
    """
    if request.node.get_closest_marker("real_provider_model_fetch"):
        return
    try:
        from hermes_cli import models as _models
    except Exception:
        return
    # raising=False for the same partially-initialized-module race documented
    # on _suppress_concurrent_hermes_gate above.
    monkeypatch.setattr(
        _models, "cached_provider_model_ids", lambda *_a, **_k: [], raising=False
    )


@pytest.fixture(autouse=True)
def _stub_lazy_feature_refresh(request, monkeypatch):
    """Neutralize ``_refresh_active_lazy_features`` for every test.

    ``cmd_update`` calls it, and it reaches ``tools.lazy_deps`` on the REAL
    interpreter: ``active_features()`` probes all 34 features / 54 specs in
    ``LAZY_DEPS`` via ``importlib.metadata``, each a distribution scan over
    site-packages (2.5s), then ``feature_missing`` re-scans per active feature,
    then ``ensure()`` runs the install path and re-scans again to verify.

    ``test_cmd_update.py`` already stubs ``_venv_pip_install`` so nothing is
    installed for real, but it deliberately left the refresh itself live "so
    the refresh stays observable for the test that asserts on it". No such test
    exists -- no test in that file references the refresh at all -- so the only
    thing still being bought is the scan cost and its side effects.

    Those side effects are the worse half. The refresh issues install commands
    through the ``subprocess.run`` mock these tests install, appending entries
    the git-only ``side_effect`` never anticipated. That desynchronizes
    assertions counting calls (``len(pull_cmds) == 1``) and produced a
    non-deterministic 2-3 failures per run, including a bare ``SystemExit: 2``
    -- failures that vanish when the affected test runs alone.

    Real coverage of the lazy-deps functions lives in
    ``tests/tools/test_lazy_deps.py``, which this directory-scoped fixture does
    not touch. Opt out with ``@pytest.mark.real_lazy_feature_refresh``.
    """
    if request.node.get_closest_marker("real_lazy_feature_refresh"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    monkeypatch.setattr(
        _cli_main, "_refresh_active_lazy_features", lambda *_a, **_k: None,
        raising=False,
    )
