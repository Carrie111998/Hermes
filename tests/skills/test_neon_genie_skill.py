"""Smoke tests for optional-skills/productivity/neon-genie.

Contract checks only (no live network / LLM):
  - SKILL.md hardline frontmatter
  - modern section order
  - referenced support paths exist
  - packaging helpers parse as Python
  - doctor / envelope authority invariants via subprocess
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "productivity"
    / "neon-genie"
)
REQUIRED_SECTIONS = (
    "# Neon Genie Skill",
    "## When to Use",
    "## Prerequisites",
    "## How to Run",
    "## Quick Reference",
    "## Procedure",
    "## Pitfalls",
    "## Verification",
)


@pytest.fixture(scope="module")
def skill_text() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(skill_text: str) -> dict:
    m = re.search(r"^---\n(.*?)\n---", skill_text, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    data = yaml.safe_load(m.group(1))
    assert isinstance(data, dict)
    return data


def test_skill_dir_exists() -> None:
    assert SKILL_DIR.is_dir(), f"missing skill dir: {SKILL_DIR}"


def test_description_hardline(frontmatter: dict) -> None:
    desc = frontmatter["description"]
    assert isinstance(desc, str)
    assert len(desc) <= 60, f"description is {len(desc)} chars (hardline ≤60): {desc!r}"
    assert desc.endswith("."), f"description must end with period: {desc!r}"
    assert "\n" not in desc.strip()
    for banned in ("powerful", "comprehensive", "seamless", "advanced"):
        assert banned not in desc.lower()


def test_author_credits_contributor_first(frontmatter: dict) -> None:
    author = str(frontmatter.get("author") or "")
    assert "Zero State" in author
    assert "scrimshawlife-ctrl" in author
    # Contributor handle/org credit first
    assert author.lower().index("zero state") < author.lower().index("alchemy")


def test_name_and_platforms(frontmatter: dict) -> None:
    assert frontmatter.get("name") == "neon-genie"
    platforms = frontmatter.get("platforms") or []
    assert set(platforms) >= {"linux", "macos", "windows"}


def test_modern_section_order(skill_text: str) -> None:
    positions = []
    for heading in REQUIRED_SECTIONS:
        idx = skill_text.find(heading)
        assert idx >= 0, f"missing section: {heading}"
        positions.append(idx)
    assert positions == sorted(positions), "sections out of required order"


def test_body_is_concise(skill_text: str) -> None:
    # Hardline target ~200 lines for complex skills; allow modest headroom
    lines = skill_text.splitlines()
    assert len(lines) <= 280, f"SKILL.md too long for optional catalog: {len(lines)} lines"


def test_advisory_authority_in_prose(skill_text: str) -> None:
    lower = skill_text.lower()
    assert "advisory" in lower
    assert "grants_execution" in lower or "never spend" in lower or "mutate" in lower


@pytest.mark.parametrize(
    "rel",
    [
        "scripts/neon_genie.py",
        "scripts/paths.py",
        "scripts/doctor.py",
        "scripts/validate_packet.py",
        "scripts/build_envelope.py",
        "scripts/lineage.py",
        "scripts/run_job.py",
        "templates/request.yaml",
        "references/gates.yaml",
        "references/hermes-runtime-contract.md",
        "references/schemas/run-envelope.schema.json",
        "references/profiles/core.md",
        "examples/product-audit.brief.yaml",
        "examples/evals/cases/zero-option.json",
    ],
)
def test_required_assets_exist(rel: str) -> None:
    path = SKILL_DIR / rel
    assert path.is_file(), f"missing required asset: {rel}"


def test_support_paths_referenced_in_skill_exist(skill_text: str) -> None:
    # File-level allowlisted refs in backticks
    refs = re.findall(
        r"`((?:references|templates|scripts|examples)/[^`\s]+)`",
        skill_text,
    )
    missing = []
    for rel in sorted(set(refs)):
        # skip bare job names mistaken as paths
        if " " in rel:
            continue
        if not (SKILL_DIR / rel).exists():
            missing.append(rel)
    assert not missing, f"SKILL.md references missing paths: {missing}"


@pytest.mark.parametrize(
    "script",
    [
        "neon_genie.py",
        "paths.py",
        "doctor.py",
        "validate_packet.py",
        "build_envelope.py",
        "lineage.py",
        "run_job.py",
        "recipe_run.py",
        "capabilities.py",
    ],
)
def test_shipped_scripts_parse(script: str) -> None:
    src = (SKILL_DIR / "scripts" / script).read_text(encoding="utf-8")
    ast.parse(src)


def test_check_and_zero_option_envelope(tmp_path: Path) -> None:
    py = sys.executable
    check = subprocess.run(
        [py, str(SKILL_DIR / "scripts" / "neon_genie.py"), "do", "check"],
        cwd=SKILL_DIR,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr + check.stdout

    out = tmp_path / "zero"
    run = subprocess.run(
        [
            py,
            str(SKILL_DIR / "scripts" / "neon_genie.py"),
            "do",
            "run",
            "--recipe",
            "zero-option",
            "--out",
            str(out),
        ],
        cwd=SKILL_DIR,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    env_path = out / "run-envelope.json"
    assert env_path.is_file()
    env = json.loads(env_path.read_text(encoding="utf-8"))
    assert env.get("authority") == "advisory_only"
    assert env.get("grants_execution") is False
    assert env.get("schema_id") == "neon-genie/run-envelope"
    assert env.get("run_id", "").startswith("ng_run_")
