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
from fnmatch import fnmatchcase
import shlex
import subprocess
import sys

import pytest

from tools.environments.base import (
    _SNAPSHOT_EXCLUDED_ENV_PATTERNS,
    _export_dump_excluding_session_vars,
)


# ---------------------------------------------------------------------------
# Unit: exclusion is decided from the variable name, never serialized values.
# ---------------------------------------------------------------------------

def test_name_filter_matches_bridged_session_vars():
    from gateway.session_context import _VAR_MAP

    for name in _VAR_MAP:
        assert any(
            fnmatchcase(name, pattern)
            for pattern in _SNAPSHOT_EXCLUDED_ENV_PATTERNS
        ), f"{name} should be excluded from the snapshot"


def test_name_filter_preserves_user_env():
    for name in (
        "PATH",
        "HOME",
        "HERMES_HOME",
        "HERMESX",
        "MY_HERMES_SESSION_ID",
    ):
        assert not any(
            fnmatchcase(name, pattern)
            for pattern in _SNAPSHOT_EXCLUDED_ENV_PATTERNS
        ), f"{name!r} must be preserved in the snapshot"


def test_export_snippet_filters_names_before_export_serialization():
    snippet = _export_dump_excluding_session_vars("/tmp/snap.tmp.$BASHPID")
    assert "compgen -e" in snippet
    assert "export -n" in snippet
    assert "export -p" in snippet
    assert 'case "${HERMES_SESSION___SNAPSHOT_VAR}"' in snippet
    assert "grep" not in snippet
    assert "/tmp/snap.tmp.$BASHPID" in snippet
    assert snippet.lstrip().startswith("{ ( while ")
    assert snippet.rstrip().endswith("> /tmp/snap.tmp.$BASHPID")


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX bash")
def test_multiline_session_metadata_cannot_escape_snapshot_filter(tmp_path):
    """A bridged value's continuation lines must never enter the snapshot."""
    snapshot = tmp_path / "snapshot.sh"
    marker = tmp_path / "injected-command-ran"
    preserved_marker = tmp_path / "preserved-value-command-ran"
    env = os.environ.copy()
    env["HERMES_SESSION_CHAT_NAME"] = (
        f"matrix room\ntouch {shlex.quote(str(marker))} #"
    )
    env["HERMES_SESSION_USER_NAME"] = "alice\nprintf ignored"
    safe_value = (
        f"first\n$(touch {shlex.quote(str(preserved_marker))})\nsecond"
    )
    env["HERMES_SAFE_MULTILINE"] = safe_value
    oldpwd_value = str(tmp_path / "previous-directory")

    dump = _export_dump_excluding_session_vars(shlex.quote(str(snapshot)))
    script = (
        f"export OLDPWD={shlex.quote(oldpwd_value)}\n"
        f"{dump}\n"
        "unset HERMES_SESSION_CHAT_NAME HERMES_SESSION_USER_NAME\n"
        f"source {shlex.quote(str(snapshot))} >/dev/null 2>&1 || true\n"
        "printf '%s' \"$HERMES_SAFE_MULTILINE\"\n"
    )
    completed = subprocess.run(
        ["bash", "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == safe_value
    assert not marker.exists(), "snapshot continuation executed as shell code"
    assert not preserved_marker.exists(), "serialized value ran command substitution"
    persisted = snapshot.read_text(encoding="utf-8")
    assert "HERMES_SESSION_CHAT_NAME" not in persisted
    assert "HERMES_SESSION_USER_NAME" not in persisted
    assert "HERMES_SAFE_MULTILINE" in persisted
    assert f'declare -x OLDPWD="{oldpwd_value}"' in persisted


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
