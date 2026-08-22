"""Runtime budget contract for the tracked root ``AGENTS.md`` file."""

from pathlib import Path

from agent.prompt_builder import build_context_files_prompt, drain_truncation_warnings

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repository_agents_file_loads_without_truncation():
    context_path = _REPO_ROOT / "AGENTS.md"
    expected = context_path.read_text(encoding="utf-8").strip()

    drain_truncation_warnings()
    prompt = build_context_files_prompt(cwd=str(_REPO_ROOT), skip_soul=True)
    warnings = drain_truncation_warnings()

    assert f"## AGENTS.md\n\n{expected}" in prompt, (
        "repository root AGENTS.md was not loaded in full"
    )
    assert not warnings, f"root project context exceeds the startup budget: {warnings}"
