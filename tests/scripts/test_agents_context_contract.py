from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.check_agents_context import (
    MAX_ROOT_CHARS,
    REQUIRED_REFERENCES,
    extract_navigable_links,
    validate_repository,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _write(repo: Path, relative: str, content: str = "# Reference\n") -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _contract_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for relative in REQUIRED_REFERENCES:
        _write(repo, relative)
    links = "\n".join(f"[{relative}]({relative})" for relative in REQUIRED_REFERENCES)
    _write(repo, "AGENTS.md", f"# Guide\n\n{links}\n")
    _write(repo, "apps/desktop/AGENTS.md", "# Desktop\n\n[Design](./DESIGN.md)\n")
    _git(repo, "add", ".")
    return repo


def _root_at_size(repo: Path, size: int) -> None:
    path = repo / "AGENTS.md"
    current = path.read_text(encoding="utf-8")
    assert len(current) <= size
    path.write_text(current + "x" * (size - len(current)), encoding="utf-8")


def test_contract_accepts_exact_root_character_limit_and_resolved_links(tmp_path):
    repo = _contract_repo(tmp_path)
    _root_at_size(repo, MAX_ROOT_CHARS)
    _git(repo, "add", "AGENTS.md")

    assert validate_repository(repo) == []


def test_contract_rejects_one_character_above_root_limit(tmp_path):
    repo = _contract_repo(tmp_path)
    _root_at_size(repo, MAX_ROOT_CHARS + 1)

    errors = validate_repository(repo)

    assert any("18,001" in error and "18,000" in error for error in errors)


@pytest.mark.parametrize(
    "replacement",
    [
        "![target](target.md)",
        "`[target](target.md)`",
        "``[target](target.md)``",
        r"\[target](target.md)",
        "<!-- [target](target.md) -->",
        "```markdown\n[target](target.md)\n```",
        "````markdown\n```\n[target](target.md)\n```\n````",
        "~~~~markdown\n[target](target.md)\n~~~~",
    ],
    ids=["image", "inline-code", "long-inline-code", "escaped", "comment", "fence", "long-fence", "tilde-fence"],
)
def test_required_reference_must_be_an_ordinary_navigable_link(tmp_path, replacement):
    repo = _contract_repo(tmp_path)
    _write(repo, "target.md")
    _write(repo, "AGENTS.md", f"# Guide\n\n{replacement}\n")
    _git(repo, "add", ".")

    errors = validate_repository(repo, required_references=("target.md",))

    assert any("required navigable link is missing: target.md" in error for error in errors)


def test_link_extractor_ignores_non_links_and_keeps_ordinary_links():
    markdown = """
[ordinary](docs/guide.md#part)
![image](image.png)
`[inline](inline.md)`
\[escaped](escaped.md)
<!-- [comment](comment.md) -->
```md
[fenced](fenced.md)
```
[external](https://example.com/x)
[anchor](#local)
"""

    assert extract_navigable_links(markdown, source="AGENTS.md") == [
        "docs/guide.md#part",
        "https://example.com/x",
        "#local",
    ]


def test_relative_link_query_and_fragment_are_stripped_for_resolution(tmp_path):
    repo = _contract_repo(tmp_path)
    _write(repo, "extra.md")
    with (repo / "AGENTS.md").open("a", encoding="utf-8") as handle:
        handle.write("\n[extra](extra.md?plain=1#section)\n")
    _git(repo, "add", ".")

    assert validate_repository(repo) == []


def test_untracked_oversized_agents_fixture_is_outside_repository_contract(tmp_path):
    repo = _contract_repo(tmp_path)
    _write(repo, "fixtures/AGENTS.md", "x" * (MAX_ROOT_CHARS + 1))

    assert validate_repository(repo) == []


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("missing.md", "does not resolve to a tracked regular file"),
        ("untracked.md", "does not resolve to a tracked regular file"),
        ("../outside.md", "escapes repository root"),
        ("docs", "does not resolve to a tracked regular file"),
    ],
)
def test_contract_rejects_invalid_relative_links(tmp_path, target, expected):
    repo = _contract_repo(tmp_path)
    if target == "untracked.md":
        _write(repo, target)
    with (repo / "AGENTS.md").open("a", encoding="utf-8") as handle:
        handle.write(f"\n[bad]({target})\n")

    errors = validate_repository(repo)

    assert any(expected in error and target in error for error in errors)


def test_contract_requires_root_and_desktop_governing_files(tmp_path):
    repo = _contract_repo(tmp_path)
    (repo / "AGENTS.md").unlink()
    (repo / "apps/desktop/AGENTS.md").unlink()

    errors = validate_repository(repo)

    assert any("required governing file is missing: AGENTS.md" in error for error in errors)
    assert any("required governing file is missing: apps/desktop/AGENTS.md" in error for error in errors)


def test_contract_fails_closed_on_non_repository_and_unreadable_utf8(tmp_path):
    repo = _contract_repo(tmp_path)
    (repo / "AGENTS.md").write_bytes(b"\xff")
    read_errors = validate_repository(repo)
    not_repo_errors = validate_repository(tmp_path / "not-a-repo")

    assert any("could not read AGENTS.md" in error for error in read_errors)
    assert any("could not enumerate tracked files" in error for error in not_repo_errors)


@pytest.mark.parametrize("content", ["```\n[link](target.md)\n", "<!-- [link](target.md)\n"])
def test_link_extractor_fails_closed_on_unterminated_exclusions(content):
    with pytest.raises(ValueError, match="unterminated"):
        extract_navigable_links(content, source="AGENTS.md")


def test_repository_agents_context_contract():
    repo_root = Path(__file__).resolve().parents[2]

    assert validate_repository(repo_root) == []
