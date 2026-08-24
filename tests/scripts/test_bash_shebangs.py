"""Tests for the portable bash shebang checker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check-bash-shebangs.py"


def run_checker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_scan_catches_markdown_and_honors_above_line_suppression(tmp_path):
    old_shebang = "#!" + "/bin/bash\n"
    markdown = tmp_path / "optional-skills" / "example.md"
    markdown.parent.mkdir()
    markdown.write_text(f"```bash\n{old_shebang}```\n", encoding="utf-8")

    result = run_checker(str(markdown))
    assert result.returncode == 1
    assert "example.md:2" in result.stdout

    # The suppression marker lives on the line ABOVE the shebang. A marker on
    # the shebang line itself would be passed to bash as an interpreter
    # argument and break execution of the very script it is meant to excuse.
    suppressed = tmp_path / "suppressed.sh"
    suppressed.write_text(
        "# shebang: ok fixture-only example\n#!" + "/bin/bash\n",
        encoding="utf-8",
    )
    result = run_checker(str(suppressed))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_scan_flags_embedded_shebang_strings_and_honors_suppression(tmp_path):
    generated = tmp_path / "generated.py"
    generated.write_text('value = "#!' + '/bin/bash\\n"\n', encoding="utf-8")

    result = run_checker(str(generated))
    assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
    assert "embedded" in result.stdout

    suppressed = tmp_path / "suppressed.py"
    suppressed.write_text(
        "# shebang: ok documents the pattern under test\n"
        'value = "#!' + '/bin/bash\\n"\n',
        encoding="utf-8",
    )
    result = run_checker(str(suppressed))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_bare_ok_without_reason_does_not_suppress(tmp_path):
    # helix4u review point: the checker must enforce that a suppression
    # carries a reason. `ok` alone, or `ok` with only whitespace/punctuation,
    # is treated as a violation so every waiver stays auditable.
    for bad_marker in (
        "# shebang: ok",
        "# shebang: OK:",
        "# shebang: ok   ",
    ):
        f = tmp_path / "bare-ok.sh"
        f.write_text(bad_marker + "\n#!" + "/bin/bash\n", encoding="utf-8")
        result = run_checker(str(f))
        assert result.returncode == 1, f"{bad_marker!r}: {result.stdout}"
        assert "shebang string" in result.stdout


def test_allowlist_skips_matched_paths_but_not_others(tmp_path):
    # helix4u review point: platform exceptions are represented OUTSIDE the
    # shebang via a path-based allowlist (e.g. a Termux shim), not by editing
    # the shebang line itself.
    allowed_file = tmp_path / "termux-shim.sh"
    allowed_file.write_text("#!" + "/bin/bash\n", encoding="utf-8")
    other_file = tmp_path / "normal.sh"
    other_file.write_text("#!" + "/bin/bash\n", encoding="utf-8")

    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text(
        "# termux launcher shim: no usable env at kernel exec time\ntermux-shim.sh\n",
        encoding="utf-8",
    )

    result = run_checker("--allowlist", str(allowlist), str(allowed_file))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    result = run_checker("--allowlist", str(allowlist), str(other_file))
    assert result.returncode == 1
    assert "normal.sh:1" in result.stdout


def test_missing_allowlist_file_fails_closed(tmp_path):
    f = tmp_path / "clean.sh"
    f.write_text("#!" + "/usr/bin/env bash\n", encoding="utf-8")
    result = run_checker("--allowlist", str(tmp_path / "nope.txt"), str(f))
    assert result.returncode == 2
    assert "cannot read allowlist" in result.stderr


def test_git_lookup_failure_in_diff_mode_exits_2(tmp_path):
    # helix4u review point: git lookup errors collapsed to an empty
    # successful scan. A broken scope query must exit 2, never 0.
    result = run_checker("--diff", "definitely-not-a-real-ref-92368")
    assert result.returncode == 2
    assert "refusing to guess" in result.stderr
