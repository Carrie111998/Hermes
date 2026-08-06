"""The lifecycle guard must not be bypassable with an embedded NUL byte.

``_read_referenced_script`` refuses to ``os.open()`` a path containing a NUL and
reports "nothing to scan". That refusal is correct, but treating it as "nothing
to scan" is exploitable at the call site: a POSIX shell *drops* NUL bytes from a
word, so ``bash danger\\x00.sh`` really executes ``danger.sh``.

The guard therefore has to scan the path the shell would resolve, not the raw
token it was handed.

These tests drive the public entry point, which is the actual attack surface,
and pass the script by name with ``cwd=`` so the command line stays free of
backslashes (``shlex`` treats those as escapes, which would silently mangle a
Windows absolute path and make the assertions meaningless).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cron.lifecycle_guard import (  # noqa: E402
    contains_gateway_lifecycle_command_or_referenced_script as guard,
)


# Assembled rather than written literally so this module's own source does not
# read as a lifecycle command to tools that scan test files.
UNSAFE_BODY = "#!/usr/bin/env bash\n" + "hermes" + " gateway " + "restart" + "\n"
SAFE_BODY = "#!/usr/bin/env bash\necho hello\n"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return script


def test_nul_in_path_does_not_bypass_the_guard(tmp_path: Path) -> None:
    """``danger\\x00.sh`` must be detected, because the shell runs ``danger.sh``."""
    _write(tmp_path, "danger.sh", UNSAFE_BODY)

    assert guard("bash danger\x00.sh", cwd=str(tmp_path)) is True


def test_plain_unsafe_script_is_still_detected(tmp_path: Path) -> None:
    """Baseline: detection of an ordinary unsafe reference is unchanged."""
    _write(tmp_path, "danger.sh", UNSAFE_BODY)

    assert guard("bash danger.sh", cwd=str(tmp_path)) is True


def test_safe_script_with_nul_is_not_flagged(tmp_path: Path) -> None:
    """Stripping the NUL must not turn a harmless script into a false positive."""
    _write(tmp_path, "safe.sh", SAFE_BODY)

    assert guard("bash safe\x00.sh", cwd=str(tmp_path)) is False


def test_nul_path_with_no_real_file_is_not_flagged(tmp_path: Path) -> None:
    """A NUL path whose stripped form does not exist stays 'nothing to scan'."""
    assert guard("bash missing\x00.sh", cwd=str(tmp_path)) is False


def test_all_nul_word_does_not_crash(tmp_path: Path) -> None:
    """A word that is only NULs strips to empty and must not raise."""
    assert guard("bash \x00\x00", cwd=str(tmp_path)) is False


def test_nul_in_source_directive_is_also_covered(tmp_path: Path) -> None:
    """The same smuggling through ``source`` must not bypass the guard either."""
    _write(tmp_path, "danger.sh", UNSAFE_BODY)

    assert guard("source danger\x00.sh", cwd=str(tmp_path)) is True


def test_guard_never_raises_on_nul_path(tmp_path: Path) -> None:
    """The guard must return a verdict, never propagate ValueError.

    Upstream already stopped ``os.open`` from crashing the guard; this asserts
    the NUL-stripping retry did not reintroduce an unguarded path operation.
    """
    _write(tmp_path, "danger.sh", UNSAFE_BODY)

    # Would raise ValueError("embedded null byte") if a path op were unguarded.
    assert guard("bash danger\x00.sh", cwd=str(tmp_path)) is True
