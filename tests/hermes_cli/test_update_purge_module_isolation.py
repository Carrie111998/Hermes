"""The in-process module purge must not strand the updater's own state.

``_purge_stale_hermes_modules`` exists so that source freshly written over
the running checkout is imported into a self-consistent module graph. It
drops EVERY cached module under the Hermes package prefixes — including
``hermes_cli.update_quiesce``, whose module-global holds the authorization
the pre-mutation quiesce established. A fresh import of that module is a
fresh ``_authorized = None``: the updater forgets, mid-update, that it
proved the fleet quiesced, and the durable relaunch bookkeeping it wrote is
suddenly owned by a module object nothing else references.

The purge runs twice per update, and the second call sits immediately
before ``_relaunch_quiesced_runtimes``. ``update_quiesce`` is therefore in
exactly the category the protected set already describes: code that is
currently EXECUTING the update. Protecting it is not a weakening — the
mutation gate still fails closed, and every source-sensitive module is
still purged.

The same purge is what made the seven update suites order-dependent when
run in one interpreter: a test that drives ``_cmd_update_impl`` evicts the
module objects the NEXT test file captured at import time, so its
``monkeypatch.setattr(pi, ...)`` patched an object no lazy importer would
ever look at again. See ``tests/hermes_cli/conftest.py``.
"""

from __future__ import annotations

import sys

import pytest

from hermes_cli import process_identity as pi
from hermes_cli import update_cmd, update_quiesce
from hermes_cli import update_inventory as ui


@pytest.fixture(autouse=True)
def _reset():
    update_quiesce.reset_mutation_authorization()
    yield
    update_quiesce.reset_mutation_authorization()


def _authorize() -> None:
    """Run a real (empty-fleet) quiesce so the authorization is genuine."""
    update_quiesce.run_pre_mutation_quiesce(
        ui.UpdatePlan(),
        stop_runtime=lambda runtime: True,
        pid_alive=lambda pid: False,
        assess_isolation=lambda plan: update_quiesce.IsolationResult(
            isolated=True, reason="test"
        ),
        persist_state=False,
        acquire_barrier=lambda: True,
        release_barrier=lambda: True,
    )


@pytest.mark.real_module_purge
class TestThePurgeKeepsTheUpdatersOwnState:
    def test_the_quiesce_authorization_survives_the_purge(self):
        _authorize()
        assert update_quiesce.authorized_report() is not None

        update_cmd._purge_stale_hermes_modules()

        from hermes_cli import update_quiesce as after_purge

        assert after_purge is update_quiesce
        assert after_purge.authorized_report() is not None
        # And the gate the mutation sites call still answers "authorized"
        # rather than aborting an update that already stopped the fleet.
        assert after_purge.assert_mutation_authorized("post-purge write")

    def test_source_sensitive_modules_are_still_purged(self):
        """The fix must not turn the purge into a no-op."""
        import hermes_cli.update_inventory  # noqa: F401  (ensure it is cached)

        assert "hermes_cli.update_inventory" in sys.modules

        update_cmd._purge_stale_hermes_modules()

        assert "hermes_cli.update_inventory" not in sys.modules


class TestTheHarnessDoesNotLetThePurgeCrossTestBoundaries:
    """Order-independence for every test that drives the real updater.

    These run in definition order in one interpreter — the shape that made
    ``test_update_serve_supervisor_fail_closed.py`` fail only when it
    followed ``test_update_quiesce_gate_recollect.py``.
    """

    @pytest.mark.real_module_purge
    def test_a_test_that_asks_for_it_still_gets_a_real_purge(self):
        update_cmd._purge_stale_hermes_modules()
        assert "hermes_cli.process_identity" not in sys.modules

    def test_the_next_test_still_owns_the_modules_it_imported(self):
        assert sys.modules.get("hermes_cli.process_identity") is pi
        assert sys.modules.get("hermes_cli.update_inventory") is ui

    def test_an_in_process_update_does_not_purge_the_test_interpreter(self):
        """The default: a purge that has nothing to reconcile is a no-op."""
        update_cmd._purge_stale_hermes_modules()
        assert sys.modules.get("hermes_cli.process_identity") is pi

    @pytest.mark.real_module_purge
    def test_the_stub_does_not_outlive_the_test_that_asked_for_it(self):
        """The neutralizing stub must not leak into ``main``'s lazy cache.

        ``main.__getattr__`` caches lazy command exports into its own
        globals, and ``monkeypatch`` captures the "old value" with
        ``getattr`` — so stubbing ``update_cmd`` before ``main`` makes
        monkeypatch capture the stub and restore IT at teardown. Every
        later test in the interpreter then calls a dead no-op, including
        the ``real_module_purge`` ones whose whole point is the real purge.
        The previous test in this class ran with the stub installed; by now
        it must be gone.
        """
        import hermes_cli.main as cli_main

        assert (
            cli_main.__dict__.get("_purge_stale_hermes_modules")
            in (None, update_cmd._purge_stale_hermes_modules)
        )
        assert (
            cli_main._purge_stale_hermes_modules
            is update_cmd._purge_stale_hermes_modules
        )
