"""Cross-session HERMES_SESSION_ID leak via the shared bash snapshot.

Regression coverage for the bug where a single long-lived backend serves many
sessions through ONE ``_active_environments["default"]`` LocalEnvironment (the
messaging gateway, TUI, and desktop/web dashboard all collapse the terminal to
"default"). That environment persists a bash *session snapshot* file and
``source``s it before every command. ``export -p`` dumped the FIRST session's
``HERMES_SESSION_ID`` into the snapshot, so every LATER session ``source``d that
stale value and its ``echo $HERMES_SESSION_ID`` reported a FOREIGN session's id
— overriding the correct per-command Popen env injected by
``_inject_session_context_env``.

The fix strips per-session bridged vars and the delegated-child process marker
from the snapshot at both dump sites in ``tools/environments/base.py``; the
current command's values are restored fresh after sourcing the snapshot.
"""

import os
import re
import sys

import pytest

from tools.environments.base import (
    _SNAPSHOT_EXCLUDED_ENV_REGEX,
    _export_dump_excluding_session_vars,
)


# ---------------------------------------------------------------------------
# Unit: the exclusion regex covers gateway-bridged and transient runtime vars.
# ---------------------------------------------------------------------------

def test_regex_matches_snapshot_transient_vars():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    # Every var the gateway bridges must be excluded.
    from gateway.session_context import _VAR_MAP

    for name in _VAR_MAP:
        line = f'declare -x {name}="whatever"'
        assert rx.search(line), f"{name} should be excluded from the snapshot"
    assert rx.search('declare -x HERMES_DELEGATED_CHILD_CONTEXT="1"')


def test_export_snippet_shape():
    snippet = _export_dump_excluding_session_vars('"$__hermes_snap_tmp"')
    assert "export -p" in snippet
    # Unset-by-name (not line-grep): multi-line declare values must not leave
    # continuation lines in the snapshot (issue #71296).
    assert "unset" in snippet
    assert "${!HERMES_SESSION_*}" in snippet
    assert "${!HERMES_CRON_AUTO_DELIVER_*}" in snippet
    assert "HERMES_UI_SESSION_ID" in snippet
    assert "HERMES_DELEGATED_CHILD_CONTEXT" in snippet
    assert "grep -vE" not in snippet
    assert '"$__hermes_snap_tmp"' in snippet
    # The redirection must be attached to a brace group wrapping the dump,
    # NOT to a pipeline segment: a redirect on a pipeline segment expands the
    # temp-path variable inside that segment's subshell (potentially
    # inconsistently with the parent that expands the follow-up ``mv``
    # operand), silently orphaning the dump and breaking snapshot env
    # persistence entirely.
    assert snippet.lstrip().startswith("{ ")
    assert "|| true; }" in snippet
    assert snippet.rstrip().endswith('> "$__hermes_snap_tmp"')


# ---------------------------------------------------------------------------
# Integration: real LocalEnvironment, two sessions, no cross-contamination.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_shared_snapshot_no_cross_session_leak(tmp_path):
    import threading

    from gateway.session_context import _VAR_MAP, _UNSET, set_session_vars
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        def run_as(sid):
            out = {}

            def worker():
                for v in _VAR_MAP.values():
                    v.set(_UNSET)
                set_session_vars(session_key="k" + sid, session_id=sid, source="desktop")
                out["r"] = env.execute('echo "[$HERMES_SESSION_ID]"')

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            return out["r"].get("output", "")

        out_a = run_as("SIDAAA")
        out_b = run_as("SIDBBB")

        assert "SIDAAA" in out_a, f"session A saw {out_a!r}"
        # The core assertion: B must see its OWN id, not A's leaked via snapshot.
        assert "SIDBBB" in out_b, f"session B saw {out_b!r}"
        assert "SIDAAA" not in out_b, f"session B leaked A's id: {out_b!r}"

        # And the snapshot file must not carry the session id at all.
        snap = env._snapshot_path
        if os.path.exists(snap):
            with open(snap) as f:
                assert "HERMES_SESSION_ID" not in f.read()
    finally:
        env.cleanup()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_delegated_child_marker_does_not_leak_from_snapshot_to_parent(tmp_path, monkeypatch):
    """A snapshot made by a delegated child must not taint later parent calls."""
    from agent.delegation_context import delegated_child_context
    from tools.environments.local import LocalEnvironment

    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    with delegated_child_context():
        env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        result = env.execute(
            'printf "marker=<%s>\\n" "${HERMES_DELEGATED_CHILD_CONTEXT:-}"'
        )

        assert result["returncode"] == 0
        assert "marker=<>" in result["output"]
        with open(env._snapshot_path) as snap:
            assert "HERMES_DELEGATED_CHILD_CONTEXT" not in snap.read()
    finally:
        env.cleanup()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_delegated_child_marker_stays_local_to_the_child_command(tmp_path, monkeypatch):
    """A child restores its live marker despite a stale snapshot."""
    from agent.delegation_context import delegated_child_context
    from tools.environments.local import LocalEnvironment

    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        with open(env._snapshot_path, "a") as snap:
            snap.write('declare -x HERMES_DELEGATED_CHILD_CONTEXT="stale"\n')

        with delegated_child_context():
            child = env.execute(
                'printf "marker=<%s>\\n" "${HERMES_DELEGATED_CHILD_CONTEXT:-}"'
            )
        parent = env.execute(
            'printf "marker=<%s>\\n" "${HERMES_DELEGATED_CHILD_CONTEXT:-}"'
        )

        assert child["returncode"] == 0
        assert "marker=<1>" in child["output"]
        assert parent["returncode"] == 0
        assert "marker=<>" in parent["output"]
        with open(env._snapshot_path) as snap:
            assert "HERMES_DELEGATED_CHILD_CONTEXT" not in snap.read()
    finally:
        env.cleanup()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_parent_command_clears_marker_from_a_preexisting_snapshot(tmp_path, monkeypatch):
    """The first parent command repairs a snapshot written by an older version."""
    from tools.environments.local import LocalEnvironment

    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        with open(env._snapshot_path, "a") as snap:
            snap.write('declare -x HERMES_DELEGATED_CHILD_CONTEXT="1"\n')

        result = env.execute(
            'printf "marker=<%s>\\n" "${HERMES_DELEGATED_CHILD_CONTEXT:-}"'
        )

        assert result["returncode"] == 0
        assert "marker=<>" in result["output"]
        with open(env._snapshot_path) as snap:
            assert "HERMES_DELEGATED_CHILD_CONTEXT" not in snap.read()
    finally:
        env.cleanup()
