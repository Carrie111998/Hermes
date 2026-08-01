"""Cron-test fixtures.

Cron tests get a second storage-isolation boundary in addition to the global
suite sandbox.  The extra boundary is intentional: ``cron.executions`` used
to cache its SQLite path at import time, before per-test fixtures could move
``HERMES_HOME``, and fixture execution rows consequently reached an operator's
live ledger when pytest inherited that home.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# An unconditional collection-time sandbox closes the case where pytest was
# launched with HERMES_HOME already pointing at the operator's profile.  A
# per-test fixture below narrows this further to one store per test.
_CRON_COLLECTION_HOME = Path(tempfile.mkdtemp(prefix="hermes-cron-tests-"))
os.environ["HERMES_HOME"] = str(_CRON_COLLECTION_HOME)
atexit.register(shutil.rmtree, _CRON_COLLECTION_HOME, True)


@pytest.fixture(autouse=True)
def _isolate_cron_execution_store(_hermetic_environment, monkeypatch, tmp_path):
    """Route every cron test to its own ledger and reject live-store writes.

    Importing here also repairs collection-order leakage: if a test module
    imported ``cron.executions`` before fixtures ran, its cached constant is
    repointed before the test body can write.  Wrapping the module's connection
    boundary then fails closed if a test later repoints the constant back to an
    operator store.
    """
    test_home = tmp_path / "cron_hermes_home"
    test_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(test_home))

    import cron.executions as executions

    test_file = test_home / "cron" / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", test_file)
    original_connect = executions._connect

    allowed_root = tmp_path.resolve()

    def guarded_connect():
        target = Path(executions.EXECUTIONS_FILE).expanduser().resolve()
        if not target.is_relative_to(allowed_root):
            raise RuntimeError(
                "cron test execution-store guard: refusing to write outside the "
                f"current test sandbox: {target}"
            )
        return original_connect()

    monkeypatch.setattr(executions, "_connect", guarded_connect)


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch):
    """Pin a default HERMES_MODEL so cron run_job tests have a resolvable model."""
    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    yield
