"""Regression tests for the macOS libmalloc stderr diagnostic filter."""

from __future__ import annotations

import os
from pathlib import Path
import select
import subprocess
import sys
import textwrap

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_filter_probe(body: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )


def test_fd_filter_suppresses_only_the_benign_malloc_warning() -> None:
    result = _run_filter_probe(
        """
        import os
        from hermes_cli.macos_stderr import _install_stderr_filter

        guard = _install_stderr_filter()
        os.write(2, b"before\\n")
        os.write(
            2,
            b"python3(123) MallocStackLogging: can't turn off malloc stack "
            b"logging because it was not enabled. extra context\\n",
        )
        os.write(
            2,
            b"python3(123) MallocStackLogging: can't turn off malloc stack "
        )
        os.write(
            2,
            b"logging because it was not enabled.\\n",
        )
        os.write(2, b"after\\n")
        guard.close()
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == (
        "before\n"
        "python3(123) MallocStackLogging: can't turn off malloc stack logging "
        "because it was not enabled. extra context\n"
        "after\n"
    )


def test_cli_installs_filter_before_other_startup_work(monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes_cli.main as main_module

    events: list[str] = []

    class StartupStopped(Exception):
        pass

    monkeypatch.setattr(
        main_module,
        "install_macos_malloc_stderr_filter",
        lambda: events.append("stderr-filter"),
        raising=False,
    )

    def stop_after_filter() -> None:
        events.append("process-title")
        raise StartupStopped

    monkeypatch.setattr(main_module, "_set_process_title", stop_after_filter)

    with pytest.raises(StartupStopped):
        main_module.main()

    assert events == ["stderr-filter", "process-title"]


@pytest.mark.skipif(sys.platform == "win32", reason="select() cannot watch Windows pipes")
def test_fd_filter_does_not_buffer_large_non_warning_stderr_lines() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    first_payload = b"x" * 4096
    second_payload = b"y" * 4096
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import os
                import sys
                from hermes_cli.macos_stderr import _install_stderr_filter

                guard = _install_stderr_filter()
                os.write(2, b"x" * 4096)
                os.write(1, b"first-ready\\n")
                sys.stdin.buffer.read(1)
                os.write(2, b"y" * 4096)
                os.write(1, b"second-ready\\n")
                sys.stdin.buffer.read(1)
                guard.close()
                """
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        assert process.stdout is not None
        assert process.stderr is not None
        assert process.stdin is not None
        assert process.stdout.readline() == b"first-ready\n"

        readable, _, _ = select.select([process.stderr], [], [], 2.0)
        assert readable, "non-warning stderr remained buffered while the filter was active"
        assert os.read(process.stderr.fileno(), len(first_payload)) == first_payload

        process.stdin.write(b"x")
        process.stdin.flush()
        assert process.stdout.readline() == b"second-ready\n"

        readable, _, _ = select.select([process.stderr], [], [], 2.0)
        assert readable, "continued non-warning stderr remained buffered before its newline"
        assert os.read(process.stderr.fileno(), len(second_payload)) == second_payload

        process.stdin.write(b"x")
        process.stdin.flush()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
