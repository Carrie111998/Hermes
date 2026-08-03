"""Runtime budget contract for tracked governing ``AGENTS.md`` files only.

Other recognized context filenames can be intentional templates or fixtures and
are outside this repository-policy guard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent.prompt_builder import (
    build_context_files_prompt,
    drain_truncation_warnings,
)
from agent.subdirectory_hints import SubdirectoryHintTracker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENTS_PATHS = ("AGENTS.md", ":(glob)**/AGENTS.md", "agents.md", ":(glob)**/agents.md")


def _repository_agents_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "--", *_AGENTS_PATHS],
        cwd=_REPO_ROOT,
        text=True,
    )
    return [_REPO_ROOT / relative for relative in output.splitlines()]


def test_tracked_repository_agents_files_load_without_truncation():
    agents_files = set(_repository_agents_files())
    required = {_REPO_ROOT / "AGENTS.md", _REPO_ROOT / "apps/desktop/AGENTS.md"}
    assert required <= agents_files, "required governing AGENTS.md files are not tracked"

    root_context_path = _REPO_ROOT / "AGENTS.md"
    assert root_context_path.is_file(), "repository root AGENTS.md is missing"
    root_context = root_context_path.read_text(encoding="utf-8").strip()

    drain_truncation_warnings()
    prompt = build_context_files_prompt(cwd=str(_REPO_ROOT), skip_soul=True)
    warnings = drain_truncation_warnings()
    assert f"## AGENTS.md\n\n{root_context}" in prompt, (
        "repository root AGENTS.md was not loaded in full"
    )
    assert not warnings, f"root project context exceeds the startup budget: {warnings}"

    for path in sorted(agents_files):
        if path.parent == _REPO_ROOT:
            continue
        relative = path.relative_to(_REPO_ROOT)
        assert path.is_file(), f"{relative} is missing"
        expected = path.read_text(encoding="utf-8").strip()
        tracker = SubdirectoryHintTracker(working_dir=str(_REPO_ROOT))
        hint = tracker.check_tool_call("read_file", {"path": str(path.parent / "probe.py")})
        assert hint is not None, f"{relative} was not discovered"
        assert expected in hint, f"{relative} was not loaded in full"
        assert "[...truncated " not in hint, (
            f"{relative} exceeds the progressive budget"
        )
