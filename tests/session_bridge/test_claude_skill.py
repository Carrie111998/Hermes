from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ASSET = ROOT / "session_bridge" / "assets" / "claude-session-bridge"
EXPECTED_FRONTMATTER = """---
name: session-bridge
description: Browse and continue the unified Claude, Codex, and Hermes session catalog.
user-invocable: true
disable-model-invocation: true
---"""


def test_install_claude_skill_uses_exact_frontmatter_and_digest(
    tmp_path: Path,
) -> None:
    from session_bridge.claude_skill import (
        claude_skill_digest,
        install_claude_skill,
    )

    claude_home = tmp_path / "claude"
    installed = install_claude_skill(claude_home)
    skill = installed / "SKILL.md"
    content = skill.read_text(encoding="utf-8")

    assert installed == claude_home / "skills" / "session-bridge"
    assert content.startswith(EXPECTED_FRONTMATTER + "\n")
    assert (
        claude_skill_digest()
        == hashlib.sha256((ASSET / "SKILL.md").read_bytes()).hexdigest()
    )
    assert hashlib.sha256(skill.read_bytes()).hexdigest() == claude_skill_digest()


def test_install_claude_skill_is_idempotent_and_preserves_unrelated_user_files(
    tmp_path: Path,
) -> None:
    from session_bridge.claude_skill import install_claude_skill

    claude_home = tmp_path / "claude"
    unrelated = claude_home / "skills" / "my-personal-skill" / "notes.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep me", encoding="utf-8")

    first = install_claude_skill(claude_home)
    second = install_claude_skill(claude_home)

    assert second == first
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    assert list((claude_home / "skills").glob("session-bridge.backup*")) == []


def test_claude_skill_uses_authenticated_catalog_tools_and_explicit_selection() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "mcp__session_bridge__session_search" in skill
    assert "mcp__session_bridge__session_get" in skill
    assert "mcp__session_bridge__session_continue" in skill
    assert "session_bridge:session_" not in skill
    for field in ("provider", "title", "cwd", "activity", "mirror", "preview"):
        assert field in skill.lower()
    assert "explicit" in skill.lower() and "selection" in skill.lower()
    assert "/resume" in skill and "Ctrl+A" in skill
    assert "/session-bridge" in skill and "global catalog" in skill.lower()
    assert "native-create" not in skill.lower()


def test_shared_asset_installer_rejects_path_traversal(tmp_path: Path) -> None:
    from session_bridge.asset_installer import AssetInstallSpec, install_packaged_asset

    with pytest.raises(ValueError, match="name|path"):
        install_packaged_asset(
            tmp_path,
            AssetInstallSpec(
                asset_name="claude-session-bridge",
                destination_name="../escape",
                files=("SKILL.md",),
                staging_marker_content=b"test\n",
            ),
        )

    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.parametrize(
    "relative",
    (
        "a:b",
        "D:/escape.txt",
        "D:escape.txt",
        "CON",
        "nested/NUL.txt",
        "nested/trailing.",
        "nested/trailing ",
    ),
)
def test_shared_asset_installer_rejects_windows_ambiguous_manifest_paths(
    tmp_path: Path, relative: str
) -> None:
    from session_bridge.asset_installer import AssetInstallSpec, install_packaged_asset

    with pytest.raises(ValueError, match="asset file path"):
        install_packaged_asset(
            tmp_path,
            AssetInstallSpec(
                asset_name="claude-session-bridge",
                destination_name="session-bridge",
                files=(relative,),
                staging_marker_content=b"test\n",
            ),
        )

    assert list(tmp_path.iterdir()) == []


def test_shared_asset_join_requires_a_strict_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge import asset_installer

    monkeypatch.setattr(asset_installer, "_validate_asset_file_path", lambda _path: None)
    with pytest.raises(ValueError, match="descendant"):
        asset_installer._strict_descendant(tmp_path / "staging", "D:/escape.txt")


def test_install_claude_skill_rejects_redirected_destination(tmp_path: Path) -> None:
    from session_bridge.claude_skill import install_claude_skill

    claude_home = tmp_path / "claude"
    skills = claude_home / "skills"
    outside = tmp_path / "outside"
    skills.mkdir(parents=True)
    outside.mkdir()
    try:
        (skills / "session-bridge").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(PermissionError, match="redirect"):
        install_claude_skill(claude_home)

    assert list(outside.iterdir()) == []


def test_claude_install_promotion_failure_restores_existing_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge import claude_skill

    claude_home = tmp_path / "claude"
    destination = claude_home / "skills" / "session-bridge"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("preserve", encoding="utf-8")
    real_replace = claude_skill._guarded_replace
    replacements: list[tuple[Path, Path]] = []

    def fail_promotion(source: Path, target: Path, identity: object) -> None:
        replacements.append((source, target))
        if target == destination and source.name.startswith(".session-bridge.install-"):
            raise PermissionError("promotion denied")
        real_replace(source, target, identity)  # type: ignore[arg-type]

    monkeypatch.setattr(claude_skill, "_guarded_replace", fail_promotion)

    with pytest.raises(PermissionError, match="promotion denied"):
        claude_skill.install_claude_skill(claude_home)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "preserve"
    assert not (destination / "SKILL.md").exists()
    assert any(
        target.name.startswith("session-bridge.backup") for _, target in replacements
    )
    assert replacements[-1][0].name.startswith("session-bridge.backup")
    assert replacements[-1][1] == destination
