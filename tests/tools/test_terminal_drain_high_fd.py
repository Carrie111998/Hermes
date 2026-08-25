"""Regression: the POSIX stdout drain must not lose output when a command's
pipe fd exceeds FD_SETSIZE (1024).

A long-lived backend (Desktop/gateway) accumulates open fds over uptime. Once a
command's stdout pipe fd is >= 1024, the drain's old ``select.select()`` call
raised ``ValueError: filedescriptor out of range in select()``; that ValueError
was caught and turned into an immediate ``break``, so the command ran and its
exit code was kept but ALL stdout/stderr was silently discarded — ``terminal``
and ``read_file`` returned an empty string with no error signal (issue #94928).
The drain now waits with ``select.poll()``, which has no FD_SETSIZE ceiling.
"""

import os
import resource
import select
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tools.environments.local import LocalEnvironment

# Enough low fds to push the next pipe fd past FD_SETSIZE (1024).
_TARGET_LOW_FDS = 1100


@pytest.mark.skipif(not hasattr(select, "poll"), reason="poll() unavailable (Windows)")
def test_terminal_output_survives_pipe_fd_above_fd_setsize():
    # Best-effort raise of the soft fd limit so we can actually hold >1024 fds;
    # skip cleanly on hosts whose hard limit is too low to reproduce.
    want = _TARGET_LOW_FDS + 200
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < want:
        new_soft = want if hard == resource.RLIM_INFINITY else min(hard, want)
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
        except (ValueError, OSError):
            pass
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < want:
            pytest.skip(f"fd limit too low to reproduce (soft={soft}, need>{want})")

    held: list[int] = []
    try:
        while len(held) < _TARGET_LOW_FDS:
            held.append(os.open(os.devnull, os.O_RDONLY))
        # Guard: if fds never crossed FD_SETSIZE the test proves nothing.
        assert held[-1] >= 1024, "setup failed to push fds above FD_SETSIZE"

        env = LocalEnvironment()
        result = env.execute("echo hello-high-fd")
    finally:
        for fd in held:
            try:
                os.close(fd)
            except OSError:
                pass

    assert result["returncode"] == 0
    assert "hello-high-fd" in result["output"]
