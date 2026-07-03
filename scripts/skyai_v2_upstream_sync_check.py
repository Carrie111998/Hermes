#!/usr/bin/env python3
"""Report whether the SkyAI v2 branch stays in plugin/skills boundaries.

This is a lightweight guard for the daily upstream-sync workflow. It does not
mutate git state. Run after fetching NousResearch/hermes-agent origin/main.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ALLOWED_PREFIXES = (
    "plugins/skyai_customer/",
    "skills/productivity/skyai-customer-hermes-v2/",
    "docs/skyai-v2-",
    "tests/plugins/test_skyai_customer_",
    "tests/scripts/test_skyai_v2_bootstrap_dev_profile.py",
    "tests/scripts/test_skyai_v2_compare_matrix.py",
    "tests/scripts/test_skyai_v2_upstream_sync_check.py",
    "scripts/skyai_v2_bootstrap_dev_profile.py",
    "scripts/skyai_v2_compare_matrix.py",
    "scripts/skyai_v2_upstream_sync_check.py",
)


def run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def changed_files(base_ref: str = "origin/main") -> list[str]:
    committed = run_git(["diff", "--name-only", f"{base_ref}...HEAD"])
    staged = run_git(["diff", "--cached", "--name-only"])
    unstaged = run_git(["diff", "--name-only"])
    untracked = run_git(["ls-files", "--others", "--exclude-standard"])
    files = {
        line.strip()
        for output in (committed, staged, unstaged, untracked)
        for line in output.splitlines()
        if line.strip()
    }
    return sorted(files)


def disallowed_files(files: list[str]) -> list[str]:
    return [
        path
        for path in files
        if not any(path == prefix or path.startswith(prefix) for prefix in ALLOWED_PREFIXES)
    ]


def upstream_counts(base_ref: str = "origin/main") -> dict[str, int | str]:
    left_right = run_git(["rev-list", "--left-right", "--count", f"HEAD...{base_ref}"])
    ahead, behind = left_right.split()
    return {"head_ahead": int(ahead), "head_behind": int(behind), "base_ref": base_ref}


def build_report(base_ref: str = "origin/main") -> dict:
    files = changed_files(base_ref)
    blocked = disallowed_files(files)
    return {
        "status": "pass" if not blocked else "fail",
        "boundary": "skyai_v2_plugin_skills_only",
        "base_ref": base_ref,
        "upstream": upstream_counts(base_ref),
        "changed_files": files,
        "disallowed_files": blocked,
        "allowed_prefixes": list(ALLOWED_PREFIXES),
    }


def main(argv: list[str]) -> int:
    base_ref = argv[1] if len(argv) > 1 else "origin/main"
    try:
        report = build_report(base_ref)
    except subprocess.CalledProcessError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
