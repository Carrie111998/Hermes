"""Delegated-child marker leak via the shared bash snapshot.

Regression coverage for the bug where a ``delegate_task`` child's terminal
command dumped ``HERMES_DELEGATED_CHILD_CONTEXT=1`` (stamped onto its Popen env
by ``_scrub_delegated_child_kanban_env``) into the shared session snapshot via
``export -p``. Every later command from ANY session then ``source``d the marker,
so the Kanban CLI/watchdog in a perfectly ordinary parent shell failed closed
with "delegate_task child contexts cannot mutate Kanban tasks or boards"
until the gateway restarted. Same bug class as the HERMES_SESSION_ID snapshot
leak (see test_snapshot_session_id_leak.py); the marker is re-stamped fresh on
every delegated command, so excluding it from the snapshot loses nothing.
"""

import os
import re
import subprocess
import sys
import tempfile

import pytest

from tools.environments.base import (
    _SNAPSHOT_EXCLUDED_ENV_REGEX,
    _export_dump_excluding_session_vars,
)
from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER


def test_regex_excludes_delegated_marker():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    line = f'declare -x {DELEGATED_CHILD_ENV_MARKER}="1"'
    assert rx.search(line), "delegated-child marker must be excluded from the snapshot"


def test_export_snippet_unsets_delegated_marker():
    snippet = _export_dump_excluding_session_vars("/tmp/snap.tmp.$BASHPID")
    assert DELEGATED_CHILD_ENV_MARKER in snippet


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_snapshot_dump_strips_delegated_marker():
    """A delegated child's env dump must not persist the lineage marker."""
    snap = tempfile.mktemp()
    snippet = _export_dump_excluding_session_vars(snap)
    env = dict(os.environ)
    env[DELEGATED_CHILD_ENV_MARKER] = "1"
    env["MY_USER_VAR"] = "keepme"
    try:
        subprocess.run(["bash", "-c", snippet], env=env, check=True)
        with open(snap) as f:
            content = f.read()
        assert DELEGATED_CHILD_ENV_MARKER not in content
        # User exports still persist — the exclusion is surgical.
        assert "MY_USER_VAR" in content
    finally:
        if os.path.exists(snap):
            os.unlink(snap)
