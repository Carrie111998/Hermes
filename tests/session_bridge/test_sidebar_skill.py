from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
ASSET = ROOT / "session_bridge" / "assets" / "session-sidebar-sync"
BASELINE = Path(__file__).parent / "fixtures" / "sidebar_skill_baseline.txt"


def _installed_files(path: Path) -> dict[str, bytes]:
    return {
        str(file.relative_to(path)).replace("\\", "/"): file.read_bytes()
        for file in path.rglob("*")
        if file.is_file()
    }


def test_sidebar_skill_baseline_records_the_verbatim_no_skill_failure() -> None:
    baseline = BASELINE.read_text(encoding="utf-8")

    assert 'prompt="Audit billing"' in baseline
    assert 'prompt="Review launch"' in baseline
    assert "No project-list call was made" in baseline


def test_sidebar_skill_contains_only_the_generated_skill_and_agent_metadata() -> None:
    files = {
        str(path.relative_to(ASSET)).replace("\\", "/")
        for path in ASSET.rglob("*")
        if path.is_file()
    }

    assert files == {"SKILL.md", "agents/openai.yaml"}


def test_sidebar_skill_metadata_matches_the_personal_codex_contract() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    metadata = (ASSET / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert skill.startswith(
        "---\nname: session-sidebar-sync\ndescription: Use when "
    )
    assert "\n---\n" in skill
    assert "TODO" not in skill
    assert metadata == (
        'interface:\n'
        '  display_name: "Session Sidebar Sync"\n'
        '  short_description: "Deliver leased Claude and Hermes sessions to the Codex sidebar"\n'
        '  default_prompt: "Run $session-sidebar-sync once and end quietly when no work is pending."\n'
    )


def test_sidebar_skill_encodes_the_single_batch_native_delivery_protocol() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "session_sidebar_pending(limit=5)" in skill
    assert "exactly once" in skill
    assert "no user-facing message" in skill
    assert "list" in skill.casefold() and "projects" in skill.casefold()
    assert "canonical local path" in skill
    assert "exact cwd" in skill
    assert "exact git root" in skill
    assert "Session Inbox" in skill
    assert "reconcile_required" in skill
    assert "recovered_thread_id" in skill
    assert "registration_prompt" in skill
    assert "exactly one native local task" in skill
    assert "rename" in skill.casefold()
    assert "session_sidebar_commit" in skill
    assert "session_sidebar_fail" in skill
    assert "error_code=<fixed code>" in skill
    assert "never `code`" in skill
    assert "exception text" in skill
    assert "every unfinished lease" in skill


def test_sidebar_skill_closes_the_baseline_and_ambiguity_loopholes() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    folded = skill.casefold()

    assert "app-server" in folded and "never" in folded
    assert "transcript" in folded and "summar" in folded
    assert "ambiguous" in folded and "duplicate" in folded
    assert "without a lease" in folded
    assert "sidebar grouping" in folded and "command cwd" in folded
    assert "first substantive continuation" in folded
    assert "session_continue" in skill
    assert 'prompt="Audit billing"' not in skill
    assert 'prompt="Review launch"' not in skill


def test_sidebar_skill_names_only_the_allowed_session_tools() -> None:
    import re

    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")
    named = set(re.findall(r"\bsession_[a-z_]+\b", skill))

    assert named == {
        "session_sidebar_pending",
        "session_sidebar_commit",
        "session_sidebar_fail",
        "session_continue",
    }


def test_resolve_codex_home_prefers_explicit_environment(tmp_path: Path) -> None:
    from session_bridge.sidebar_skill import resolve_codex_home

    selected = tmp_path / "portable-codex"

    assert resolve_codex_home({"CODEX_HOME": str(selected)}) == selected


def test_resolve_codex_home_defaults_below_the_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge.sidebar_skill import resolve_codex_home

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert resolve_codex_home({}) == tmp_path / ".codex"


def test_install_sidebar_skill_copies_packaged_asset_and_is_idempotent(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    first = install_sidebar_skill(codex_home)
    second = install_sidebar_skill(codex_home)

    assert first == codex_home / "skills" / "session-sidebar-sync"
    assert second == first
    assert _installed_files(first) == _installed_files(ASSET)
    assert list((codex_home / "skills").glob("session-sidebar-sync.backup*")) == []


def test_install_sidebar_skill_backs_up_different_content_without_collision(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    destination = codex_home / "skills" / "session-sidebar-sync"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("first", encoding="utf-8")
    install_sidebar_skill(codex_home)
    shutil.rmtree(destination)
    destination.mkdir()
    (destination / "old.txt").write_text("second", encoding="utf-8")

    install_sidebar_skill(codex_home)

    backups = sorted((codex_home / "skills").glob("session-sidebar-sync.backup*"))
    assert len(backups) == 2
    assert {(backup / "old.txt").read_text(encoding="utf-8") for backup in backups} == {
        "first",
        "second",
    }
    assert _installed_files(destination) == _installed_files(ASSET)


def test_install_sidebar_skill_copy_failure_preserves_existing_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge import sidebar_skill

    codex_home = tmp_path / "codex"
    destination = codex_home / "skills" / "session-sidebar-sync"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(
        sidebar_skill,
        "_copy_packaged_skill",
        lambda _destination: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with pytest.raises(PermissionError, match="denied"):
        sidebar_skill.install_sidebar_skill(codex_home)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "preserve"
    assert not list((codex_home / "skills").glob(".session-sidebar-sync.install-*"))


def test_install_sidebar_skill_refuses_redirected_destination(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    skills = codex_home / "skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    skills.mkdir(parents=True)
    destination = skills / "session-sidebar-sync"
    try:
        destination.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(PermissionError, match="redirect"):
        install_sidebar_skill(codex_home)

    assert list(outside.iterdir()) == []


def test_install_sidebar_skill_refuses_redirected_parent_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    redirected_parent = tmp_path / "redirected-parent"
    redirected_parent.mkdir()
    real_lstat = os.lstat

    def redirect_aware_lstat(path: os.PathLike[str] | str):
        if Path(path).absolute() == redirected_parent.absolute():
            return SimpleNamespace(st_mode=0o120777, st_file_attributes=0)
        return real_lstat(path)

    monkeypatch.setattr(os, "lstat", redirect_aware_lstat)

    with pytest.raises(PermissionError, match="redirect"):
        install_sidebar_skill(redirected_parent / "codex")

    assert list(redirected_parent.iterdir()) == []


def test_install_sidebar_skill_serializes_concurrent_repeated_installs(
    tmp_path: Path,
) -> None:
    from session_bridge.sidebar_skill import install_sidebar_skill

    codex_home = tmp_path / "codex"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: install_sidebar_skill(codex_home), range(8)))

    assert len(set(results)) == 1
    assert _installed_files(results[0]) == _installed_files(ASSET)
    assert not list((codex_home / "skills").glob(".session-sidebar-sync.install-*"))


@pytest.mark.skipif(
    "built_wheel" not in " ".join(sys.argv),
    reason="run explicitly because a full repository wheel exceeds focused timeout",
)
@pytest.mark.timeout(180)
def test_built_wheel_contains_the_sidebar_skill_assets(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["UV_NO_PROGRESS"] = "1"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    wheel = next(tmp_path.glob("*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert "session_bridge/assets/session-sidebar-sync/SKILL.md" in names
    assert "session_bridge/assets/session-sidebar-sync/agents/openai.yaml" in names
