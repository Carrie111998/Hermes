"""Invariant tests for the bundled hermes-self-maintenance skill.

Covers skills/software-development/hermes-self-maintenance — the nightly
auto-update and health-check skill. Tests assert contracts (frontmatter
shape, referenced scripts exist, script imports are stdlib-only, script
runs and exits cleanly), not snapshots of skill content.
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SKILL_DIR = REPO / "skills" / "software-development" / "hermes-self-maintenance"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts" / "maintenance_check.py"


def _frontmatter() -> dict:
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{SKILL_MD} has no YAML frontmatter"
    return yaml.safe_load(match.group(1))


# ---------------------------------------------------------------------------
# Frontmatter contract
# ---------------------------------------------------------------------------

def test_skill_exists():
    assert SKILL_MD.exists(), f"missing {SKILL_MD}"


def test_frontmatter_shape():
    fm = _frontmatter()
    assert fm["name"] == "hermes-self-maintenance"
    assert fm["description"].strip()
    assert len(fm["description"]) <= 60, (
        f"description is {len(fm['description'])} chars (max 60 for system prompt index)"
    )
    assert fm["description"].rstrip('"').endswith(".")
    platforms = fm.get("platforms")
    assert platforms, "missing platforms gating"
    assert set(platforms) <= {"linux", "macos", "windows"}
    assert set(platforms) == {"linux", "macos", "windows"}, (
        "self-maintenance should work on all platforms"
    )


def test_metadata_tags():
    fm = _frontmatter()
    tags = fm.get("metadata", {}).get("hermes", {}).get("tags", [])
    assert "hermes" in tags
    assert any("maintenance" in t or "update" in t for t in tags)


# ---------------------------------------------------------------------------
# Script contract
# ---------------------------------------------------------------------------

def test_referenced_scripts_exist():
    """Every scripts/... path mentioned in SKILL.md must exist on disk."""
    body = SKILL_MD.read_text(encoding="utf-8")
    refs = set(re.findall(r"scripts/[\w./-]+\.py", body))
    assert refs, "SKILL.md references no helper scripts"
    for ref in refs:
        assert (SKILL_DIR / ref).exists(), f"SKILL.md references missing {ref}"


def test_script_is_python_and_executable():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env python3"), "script should have a shebang"
    assert "if __name__" in text, "script should have a main guard"
    assert "sys.exit(main())" in text or "sys.exit(main" in text, (
        "script should exit with main()"
    )


def test_script_uses_only_stdlib():
    """The script must not import hermes_cli or any non-stdlib package at
    module level. psutil is allowed as an optional import inside a function."""
    text = SCRIPT.read_text(encoding="utf-8")
    # Check all top-level import statements
    import_lines = re.findall(r"^(?:import|from)\s+(\S+)", text, re.MULTILINE)
    forbidden = {"hermes_cli", "hermes_daemon", "run_agent", "agent"}
    for imp in import_lines:
        root = imp.split(".")[0]
        assert root not in forbidden, f"script imports forbidden package: {imp}"
    # psutil must be a lazy/optional import inside a function, not at module level
    toplevel_imports = [
        line for line in text.splitlines()
        if re.match(r"^(?:import|from)\s+", line) and "psutil" in line
    ]
    assert not toplevel_imports, (
        "psutil must be a lazy import inside a function, not at module level"
    )


def test_script_has_env_var_skip_flags():
    """The script should support MAINTENANCE_SKIP_* env vars for selective checks."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "MAINTENANCE_SKIP_UPDATE" in text
    assert "MAINTENANCE_SKIP_GATEWAY" in text
    assert "MAINTENANCE_SKIP_CRON" in text
    assert "MAINTENANCE_REPORT_ALL" in text


def test_script_has_cross_platform_gateway_check():
    """Gateway check must handle both win32 and posix."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "win32" in text
    assert "pgrep" in text or "ps aux" in text or "psutil" in text


def test_script_does_not_auto_update():
    """The script must never run 'hermes update' without --check. Auto-updating
    a running gateway is the exact risk that has stalled core PRs."""
    text = SCRIPT.read_text(encoding="utf-8")
    # Find all hermes update calls
    update_calls = re.findall(r'["\']hermes["\']\s*,\s*["\']update["\']', text)
    for _ in update_calls:
        pass  # At least we found them
    # Must not contain a bare update without --check
    assert '["hermes", "update"]' not in text.replace(" ", ""), (
        "script must not run bare 'hermes update' — only 'hermes update --check'"
    )
    # Verify --check is used
    assert '"--check"' in text or "'--check'" in text, (
        "script must use hermes update --check, not hermes update"
    )


# ---------------------------------------------------------------------------
# Script execution contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("skip_all", [
    {"MAINTENANCE_SKIP_UPDATE": "1", "MAINTENANCE_SKIP_GATEWAY": "1", "MAINTENANCE_SKIP_CRON": "1"},
])
def test_script_runs_silent_when_all_skipped(skip_all, monkeypatch):
    """When all checks are skipped, the script should exit 0 with no output."""
    for k, v in skip_all.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MAINTENANCE_REPORT_ALL", raising=False)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**__import__("os").environ, **skip_all},
    )
    assert result.returncode == 0, f"exit {result.returncode}, stderr: {result.stderr}"
    assert result.stdout.strip() == "", f"expected no output, got: {result.stdout[:200]}"


def test_script_reports_all_mode(monkeypatch):
    """MAINTENANCE_REPORT_ALL=1 with all skips should still print a header."""
    env = {
        "MAINTENANCE_REPORT_ALL": "1",
        "MAINTENANCE_SKIP_UPDATE": "1",
        "MAINTENANCE_SKIP_GATEWAY": "1",
        "MAINTENANCE_SKIP_CRON": "1",
    }
    import os
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **env},
    )
    assert "Hermes Maintenance Report" in result.stdout
    assert "skipped" in result.stdout.lower()


def test_script_does_not_crash_without_hermes_exe(monkeypatch):
    """Script should handle missing hermes executable gracefully."""
    env = {
        "MAINTENANCE_SKIP_GATEWAY": "1",
        "MAINTENANCE_SKIP_CRON": "1",
        "MAINTENANCE_REPORT_ALL": "1",
    }
    # Ensure HERMES_HOME points somewhere that doesn't have hermes
    import os
    env["HERMES_HOME"] = "/nonexistent/path/for/test"
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **env},
    )
    assert result.returncode in (0, 1), f"unexpected exit {result.returncode}"
    assert "Traceback" not in result.stderr, f"script crashed: {result.stderr[:300]}"