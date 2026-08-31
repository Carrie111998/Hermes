"""Fixtures and isolation shared across the hermes_cli tests."""

from __future__ import annotations

import sys

import pytest


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


#: Package roots the updater's ``_purge_stale_hermes_modules`` evicts.
_HERMES_MODULE_ROOTS = ("hermes_cli", "gateway", "tools", "tui_gateway", "agent")


@pytest.fixture(autouse=True)
def _neutralize_the_updaters_module_purge(request, monkeypatch):
    """Keep an in-process update from purging THIS interpreter's modules.

    ``hermes update`` runs in the pre-update interpreter, so before it
    imports freshly-written source it evicts every cached Hermes module —
    ``_purge_stale_hermes_modules``. In production that is the whole point:
    the checkout on disk changed, and the next lazy import must rebuild a
    self-consistent graph from it.

    Under a test that drives ``_cmd_update_impl`` in-process, nothing on
    disk changed, so the purge reconciles nothing — and it evicts the very
    module objects the NEXT test file captured at import time. Their
    ``monkeypatch.setattr(pi, ...)`` then patches an object no lazy
    ``from hermes_cli.x import y`` will ever look at again: the patch does
    nothing, and the test silently reads the real probe's answer.

    That is what made the seven update suites order-dependent in one
    interpreter. ``test_update_serve_supervisor_fail_closed.py`` passes
    alone; after ``test_update_quiesce_gate_recollect.py`` (which drives
    the real update end to end) its unprovable-spawner patch is dead and
    the row comes back ``manual-serve`` instead of ``desktop``.

    Restoring the cache afterwards does not work — a module first imported
    during the purged window bound its own references to the replacement
    graph, so any partial restore hands the next test a mix of both. The
    deterministic answer is to not perform a purge that has nothing to
    reconcile. Tests of the purge itself opt back in with
    ``@pytest.mark.real_module_purge``.
    """
    if request.node.get_closest_marker("real_module_purge"):
        # Opt-in: run the real purge, but keep its blast radius inside this
        # test. Exact here, because a test that calls the purge directly
        # imports nothing during the window — the mixed graph only appears
        # when a whole update runs inside it, which is what the default
        # branch below prevents.
        snapshot = {
            name: module
            for name, module in list(sys.modules.items())
            if name.split(".", 1)[0] in _HERMES_MODULE_ROOTS
        }
        yield
        for name in [
            name
            for name in list(sys.modules)
            if name.split(".", 1)[0] in _HERMES_MODULE_ROOTS
            and name not in snapshot
        ]:
            sys.modules.pop(name, None)
        sys.modules.update(snapshot)
        return
    from hermes_cli import update_cmd

    try:
        from hermes_cli import main as _cli_main
    except Exception:
        _cli_main = None
    # main.__getattr__ caches lazy command exports into its own globals on
    # first read, and `_m()` reads them from there — so the proxy has to be
    # patched too, not just the module it forwards to.
    #
    # Patch the proxy FIRST, and never the other way round. monkeypatch
    # records the old value with `getattr`, which on a not-yet-cached lazy
    # export runs `main.__getattr__` — that resolves the attribute out of
    # `update_cmd` and caches whatever it finds. Patch `update_cmd` first
    # and the value it finds is our stub, so monkeypatch "restores" the
    # stub into `main`'s globals at teardown and every later test in the
    # interpreter — including the `real_module_purge` ones that need the
    # genuine purge — silently calls a dead no-op.
    if _cli_main is not None:
        monkeypatch.setattr(
            _cli_main, "_purge_stale_hermes_modules", lambda: None, raising=False
        )
    monkeypatch.setattr(
        update_cmd, "_purge_stale_hermes_modules", lambda: None, raising=False
    )
    yield
