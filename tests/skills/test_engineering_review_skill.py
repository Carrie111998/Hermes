"""Production checks for the engineering-review bundled skill."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "software-development" / "engineering-review"
COLLECTOR = SKILL_DIR / "scripts" / "collect_hermes_review_evidence.py"


def load_collector():
    spec = importlib.util.spec_from_file_location("engineering_review_collector", COLLECTOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_engineering_review_skill_frontmatter_and_components() -> None:
    content = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert content.startswith("---")
    match = re.search(r"\n---\s*\n", content[3:])
    assert match, "SKILL.md missing closing frontmatter delimiter"
    frontmatter = yaml.safe_load(content[3 : match.start() + 3])

    assert frontmatter["name"] == "engineering-review"
    assert frontmatter["description"].startswith("Use when")
    assert frontmatter["version"] == "1.1.1"

    for rel in [
        "references/system-role.md",
        "references/review-contract.md",
        "references/execution-workflow.md",
        "references/engineering-checklist.md",
        "references/tool-permissions.md",
        "templates/engineering-review-report.md",
        "scripts/collect_hermes_review_evidence.py",
    ]:
        assert (SKILL_DIR / rel).is_file(), rel


def test_collector_schema_and_secret_safety(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run_agent.py").write_text("# agent loop\n", encoding="utf-8")
    (repo / "tools").mkdir()
    (repo / "tools" / "registry.py").write_text("# registry\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_example.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (repo / ".env").write_text("API_KEY=super-secret-value\n", encoding="utf-8")
    skill = repo / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Use when testing.\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    collector = load_collector()
    data = collector.collect(repo)
    serialized = json.dumps(data, ensure_ascii=False)

    assert data["schema_version"] == "1.0"
    assert data["collector"]["version"] == "1.1.1"
    assert data["inventory"]["test_files"] == 1
    assert data["skill_frontmatter_health"]["invalid_frontmatter_count"] == 0
    assert "suggested_review_commands" in data
    assert "super-secret-value" not in serialized


def test_collector_skips_symlinked_files_and_directories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "SKILL.md"
    secret.write_text(
        "---\nname: leaked-secret\ndescription: super-secret-value\n---\n",
        encoding="utf-8",
    )
    skill_dir = repo / "skills" / "linked"
    skill_dir.mkdir(parents=True)
    try:
        (skill_dir / "SKILL.md").symlink_to(secret)
        (repo / "linked-directory").symlink_to(outside, target_is_directory=True)
        (repo / "run_agent.py").symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    collector = load_collector()
    data = collector.collect(repo)
    serialized = json.dumps(data, ensure_ascii=False)

    assert data["skill_frontmatter_health"]["skill_files_checked"] == 0
    assert "leaked-secret" not in serialized
    assert "super-secret-value" not in serialized
    agent_loop = data["subsystems"]["agent_loop"][0]
    assert agent_loop["exists"] is False
    assert agent_loop["skipped"] == "symlink"


def test_collector_reports_invalid_skill_frontmatter(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    bad_skill = repo / "skills" / "bad"
    bad_skill.mkdir(parents=True)
    (bad_skill / "SKILL.md").write_text("# Missing frontmatter\n", encoding="utf-8")

    collector = load_collector()
    data = collector.collect(repo)

    assert data["skill_frontmatter_health"]["invalid_frontmatter_count"] == 1
    assert data["skill_frontmatter_health"]["invalid_frontmatter"][0]["path"] == "skills/bad/SKILL.md"
