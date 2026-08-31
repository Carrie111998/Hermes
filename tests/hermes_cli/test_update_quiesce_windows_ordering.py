"""Pre-mutation quiesce ordering on Windows.

Same contract as the Linux lane, exercised on this host's real process
and signal semantics: every inventoried runtime is stopped and observed
gone BEFORE the checkout moves, and each replacement is a fresh
interpreter reading the NEW source. Split per OS (rather than one test
walking platforms, or monkeypatching ``sys.platform``) so the host itself
proves it; the scenario bodies are shared in ``quiesce_fleet_support``.
"""

from __future__ import annotations

import pytest

from hermes_cli import update_quiesce
from tests.hermes_cli import quiesce_fleet_support as support

pytestmark = pytest.mark.windows_only


@pytest.fixture(autouse=True)
def _clean_state():
    update_quiesce.reset_mutation_authorization()
    update_quiesce.clear_restart_pending_state()
    yield
    update_quiesce.reset_mutation_authorization()
    update_quiesce.clear_restart_pending_state()


def test_runtimes_stop_before_head_moves(tmp_path):
    support.assert_runtimes_stop_before_head_moves(tmp_path)


def test_failed_stop_never_authorizes_mutation(tmp_path):
    support.assert_failed_stop_never_authorizes_mutation(tmp_path)


def test_replacement_reads_the_new_source(tmp_path):
    support.assert_replacement_reads_the_new_source(tmp_path)
