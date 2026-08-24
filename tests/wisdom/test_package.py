import json
from pathlib import Path

import pytest

from hermes_wisdom.package import (
    PackagePolicyError,
    prepare_package,
    verify_content_files,
)


def make_skill(root: Path) -> Path:
    skill = root / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Test.\n---\n\n# Test\n", encoding="utf-8"
    )
    refs = skill / "refs"
    refs.mkdir()
    (refs / "notes.txt").write_text("Exact notes.\n", encoding="utf-8")
    return skill


def test_preparation_creates_only_instruction_overlay_and_hashes(tmp_path: Path):
    skill = make_skill(tmp_path)
    package = prepare_package(
        skill,
        overlay_root=tmp_path / "overlays",
        author_description="  <b>Does</b> the useful thing. \r\n",
        owner="owner",
        installation_id="installation-123456",
    )
    assert package.description == "Does the useful thing."
    assert {item.path for item in package.files} == {
        "SKILL.md",
        "refs/notes.txt",
        "skill.manifest.json",
    }
    manifest = json.loads(
        (package.overlay / "skill.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["requirements"]["tools"] == []
    assert package.content_hash.startswith("sha256:")


@pytest.mark.parametrize(
    "relative,content",
    [
        ("scripts/run.sh", "echo nope"),
        ("templates/active.md", "active"),
        ("package.json", "{}"),
        ("refs/package.json", "{}"),
    ],
)
def test_unsupported_content_is_rejected_not_silently_omitted(
    tmp_path: Path, relative: str, content: str
):
    skill = make_skill(tmp_path)
    target = skill / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    with pytest.raises(PackagePolicyError):
        prepare_package(
            skill,
            overlay_root=tmp_path / "overlays",
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
        )


def test_download_rejects_hostile_paths_modes_and_binary():
    manifest = b'{"schema_version":1}'
    base = [("SKILL.md", "file", b"# test"), ("skill.manifest.json", "file", manifest)]
    with pytest.raises(PackagePolicyError):
        verify_content_files(base + [("../escape", "file", b"x")])
    with pytest.raises(PackagePolicyError):
        verify_content_files([
            (name, "exec" if name == "SKILL.md" else mode, body)
            for name, mode, body in base
        ])
    with pytest.raises(PackagePolicyError):
        verify_content_files(base + [("assets/image.bin", "file", b"\xff\xfe")])


def test_referenced_script_requires_explicit_instruction_only_fork(tmp_path: Path):
    skill = make_skill(tmp_path)
    (skill / "SKILL.md").write_text("Run scripts/deploy.sh now.", encoding="utf-8")
    with pytest.raises(PackagePolicyError, match="instruction-only fork"):
        prepare_package(
            skill,
            overlay_root=tmp_path / "overlays",
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
        )
