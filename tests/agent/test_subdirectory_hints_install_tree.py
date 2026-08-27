"""Install-tree refusal: subdirectory hints must never seed from the Hermes package root.

Companion to the prompt_builder fallback guard (#64590) — see #85668. When a
desktop/gateway session falls back into the install tree, the tracker must
re-home to the user's home directory instead of treating the repo's
contributor AGENTS.md files as project context.
"""

from pathlib import Path

from agent.subdirectory_hints import SubdirectoryHintTracker, _is_install_tree

# The real package root: whatever checkout is running these tests.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def test_install_tree_detection():
    assert _is_install_tree(PACKAGE_ROOT)
    assert _is_install_tree(PACKAGE_ROOT / "agent" / "subdir")
    # Ancestors of the package root are legitimate workspaces.
    parent = PACKAGE_ROOT.parent
    if parent != PACKAGE_ROOT:
        assert not _is_install_tree(parent)


def test_working_dir_inside_install_tree_falls_back_to_home(tmp_path, monkeypatch):
    # Explicitly point the tracker at a directory inside the install tree.
    monkeypatch.chdir(PACKAGE_ROOT)
    tracker = SubdirectoryHintTracker(working_dir=str(PACKAGE_ROOT / "agent"))
    assert tracker.working_dir == Path.home().resolve()
    # Nothing from the install tree may be marked loaded.
    assert tracker.working_dir not in (
        PACKAGE_ROOT,
        PACKAGE_ROOT / "agent",
    )


def test_working_dir_explicit_install_tree_root_falls_back_to_home():
    tracker = SubdirectoryHintTracker(working_dir=str(PACKAGE_ROOT))
    assert tracker.working_dir == Path.home().resolve()


def test_legitimate_workspace_outside_install_tree_is_kept(tmp_path):
    ws = tmp_path / "my-project"
    ws.mkdir()
    tracker = SubdirectoryHintTracker(working_dir=str(ws))
    assert tracker.working_dir == ws.resolve()


def test_home_fallback_does_not_inject_repo_agents_md(tmp_path, monkeypatch):
    """E2E shape: cwd = install tree, tracker must never return repo hints."""
    monkeypatch.chdir(PACKAGE_ROOT)
    tracker = SubdirectoryHintTracker(working_dir=None)  # None → runtime cwd resolution
    # Whatever it resolved to, it must not be inside the install tree...
    assert not _is_install_tree(tracker.working_dir)
    # ...and a probe into the install tree must yield no hints (outside tree).
    hints = tracker.check_tool_call(
        "read_file", {"path": str(PACKAGE_ROOT / "AGENTS.md")}
    )
    assert hints is None or "Hermes Agent - Development Guide" not in hints
