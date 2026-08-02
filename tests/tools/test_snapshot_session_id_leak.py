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

def test_export_snippet_shape():
    snippet = _export_dump_excluding_session_vars("/tmp/snap.tmp.$BASHPID")
    assert "export -p" in snippet
    # Unset-by-name (not line-grep): multi-line declare values must not leave
    # continuation lines in the snapshot (issue #71296).
    assert "unset" in snippet
    assert "${!HERMES_SESSION_*}" in snippet
    assert "${!HERMES_CRON_AUTO_DELIVER_*}" in snippet
    assert "HERMES_UI_SESSION_ID" in snippet
    assert "HERMES_DELEGATED_CHILD_CONTEXT" in snippet
    assert "grep -vE" not in snippet
    assert "/tmp/snap.tmp.$BASHPID" in snippet
    # The redirection must be attached to a brace group wrapping the dump,
    # NOT to a pipeline segment: a redirect on a pipeline segment expands
    # $BASHPID inside that segment's subshell (a different PID than the parent
    # that expands the follow-up ``mv`` operand), silently orphaning the dump
    # and breaking snapshot env persistence entirely.
    assert snippet.lstrip().startswith("{ ")
    assert "|| true; }" in snippet
    assert snippet.rstrip().endswith("> /tmp/snap.tmp.$BASHPID")


def test_real_snapshot_excludes_delegated_child_marker(tmp_path, monkeypatch):
    """Real regression: when the marker is live in the env, the produced
    snapshot must NOT export it after the unset runs.

    Exercises the actual dump path (not just the regex) with
    HERMES_DELEGATED_CHILD_CONTEXT=1 set via monkeypatch (so any prior value
    is preserved and restored), then evaluates the snippet in a real POSIX
    shell to prove the marker is gone from the captured snapshot. Uses a
    unique tmp_path-derived output so parallel test runs never collide, and
    asserts the capture/read commands succeed.

    Skips where no POSIX shell is available (pure Windows cmd).
    """
    import shutil
    import stat
    import subprocess

    bash = (
        shutil.which("git-bash")
        or shutil.which("bash")
        or shutil.which("wsl")
    )
    if bash is None:
        pytest.skip("no POSIX shell available to evaluate snapshot snippet")

    # Set the marker via monkeypatch: preserves any prior value and restores
    # it after the test (the snippet's unset must not leak into our process).
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")

    # git-bash on Windows only honors POSIX-style paths for the `>` redirect
    # target, so map tmp_path (Windows) to a git-bash root path under /tmp.
    snap_posix = f"/tmp/hermes_snap_{os.getpid()}_{id(tmp_path)}.out"
    snippet = _export_dump_excluding_session_vars(snap_posix)

    script = tmp_path / "run_snapshot.sh"
    script.write_text(snippet + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    run = subprocess.run([bash, str(script)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, f"snapshot snippet failed: {run.stderr!r}"

    # Read the captured snapshot back through bash (it wrote to the POSIX path
    # inside the git-bash root). Assert the command itself succeeded.
    cat = subprocess.run(
        [bash, "-c", f"cat {snap_posix}"],
        capture_output=True, text=True, timeout=30,
    )
    assert cat.returncode == 0, f"snapshot read failed: {cat.stderr!r}"
    captured_text = cat.stdout
    assert "HERMES_DELEGATED_CHILD_CONTEXT" not in captured_text, (
        f"delegated-child marker leaked into snapshot: {captured_text!r}"
    )

    # The marker must NOT have been removed from our own process env.
    assert os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT") == "1", (
        "test mutated the process-global env instead of only the snapshot"
    )



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
