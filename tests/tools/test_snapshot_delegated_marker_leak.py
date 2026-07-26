"""Delegated-child marker leaking into the shared bash snapshot (issue #71941).

``delegate_task`` children run inside ``delegated_child_context()``, and
``delegated_child_subprocess_env()`` materializes
``HERMES_DELEGATED_CHILD_CONTEXT=1`` into the env of every subprocess spawned
while that context is active. ``tools/terminal_tool.py::_resolve_container_task_id``
deliberately collapses ordinary delegated children onto the ``"default"``
environment key, so parent and child share ONE ``LocalEnvironment`` — and one
bash session snapshot.

``export -p`` exports the whole process env, so the child's marker was dumped
into that shared snapshot. The next *ordinary* command sourced the declaration
and was indistinguishable from a delegated child to the Kanban mutation guards
(``hermes_cli/kanban.py``, ``hermes_cli/kanban_db.py``), which deny
dispatcher-owned mutations for delegated children. Because that command
re-dumped the snapshot, the stale marker survived for the lifetime of the
cached environment.

The fix adds the marker to ``_SNAPSHOT_EXCLUDED_ENV_REGEX`` in
``tools/environments/base.py``, alongside the per-session bridged vars. It is
lossless: a genuinely delegated spawn gets the marker re-injected by
``delegated_child_subprocess_env`` every time.
"""

import sys

import pytest

from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER,
    delegated_child_context,
)
from tools.environments.base import _export_dump_excluding_session_vars


# ---------------------------------------------------------------------------
# Unit: the emitted dump filters the marker by name.
#
# Asserted against the generated shell snippet rather than the filter constant
# itself: #71464 proposes replacing the line regex with shell ``case`` globs,
# and this regression is orthogonal to how the filtering is expressed.
# ---------------------------------------------------------------------------

def test_dump_snippet_filters_the_marker_by_name():
    snippet = _export_dump_excluding_session_vars("/tmp/snap.tmp.$BASHPID")
    assert DELEGATED_CHILD_ENV_MARKER in snippet, (
        "the snapshot dump does not filter the delegation marker"
    )


# ---------------------------------------------------------------------------
# Integration: real LocalEnvironment, delegated call then ordinary call.
# ---------------------------------------------------------------------------

def _probe(env):
    """Report whether the marker is set in the command's shell, and its value."""
    result = env.execute(
        f'printf "presence=%s value=%s\\n" '
        f'"${{{DELEGATED_CHILD_ENV_MARKER}+set}}" '
        f'"${{{DELEGATED_CHILD_ENV_MARKER}-unset}}"'
    )
    return result.get("output", "").strip()


def _snapshot_has_marker(env) -> bool:
    with open(env._snapshot_path, encoding="utf-8", errors="replace") as fh:
        return DELEGATED_CHILD_ENV_MARKER in fh.read()


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    from tools.environments.local import LocalEnvironment

    # A delegated ancestor in the real process env would make every command
    # look delegated regardless of the snapshot.
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        yield env
    finally:
        env.cleanup()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_delegated_marker_does_not_survive_into_the_next_command(local_env):
    """The reproduction from issue #71941, start to finish."""
    assert not _snapshot_has_marker(local_env), "marker present before delegation"

    with delegated_child_context():
        child_output = _probe(local_env)

    # The feature still works: the delegated command itself sees the marker.
    assert child_output == "presence=set value=1", child_output
    assert not _snapshot_has_marker(local_env), (
        "the delegated command persisted the marker into the shared snapshot"
    )

    # The ordinary command that follows must not inherit it.
    parent_output = _probe(local_env)
    assert parent_output == "presence= value=unset", parent_output
    assert not _snapshot_has_marker(local_env)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_marker_excluded_from_the_bootstrap_snapshot(tmp_path, monkeypatch):
    """init_session dumps the snapshot too — cover that site as well.

    An environment first created *during* a delegated turn writes its initial
    snapshot from an env that already carries the marker.
    """
    from tools.environments.local import LocalEnvironment

    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    with delegated_child_context():
        env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
        try:
            env.init_session()
            assert not _snapshot_has_marker(env), (
                "the bootstrap snapshot captured the delegation marker"
            )
        finally:
            env.cleanup()
