"""Every sales run type must reach a skill that exists and says what it does.

server/run_types.py is the dispatcher's single source of truth: a run type maps
to a directory under skills/sales/ and a prompt builder. Nothing at import time
checks that the directory is really there, so a renamed skill fails at run time
as `hermes --skills <name>` exiting non-zero, minutes into a tenant's run, with
no signal about which mapping went stale. These tests move that failure to CI.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from server.run_types import READ_ONLY, REGISTRY, SEND_TYPES, build


REPO = Path(__file__).resolve().parents[2]
SALES_SKILLS = REPO / "skills" / "sales"

# analytics_refresh is pure DB aggregation and deliberately maps to no skill.
AGENT_RUN_TYPES = sorted(name for name, (skill, _) in REGISTRY.items() if skill)


def _frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill_md} has no YAML frontmatter"
    _, block, _ = text.split("---\n", 2)
    return yaml.safe_load(block) or {}


def test_sales_skills_directory_is_not_empty() -> None:
    assert SALES_SKILLS.is_dir(), "skills/sales/ is the product's agent surface"
    assert list(SALES_SKILLS.glob("*/SKILL.md")), "no sales skills found"


@pytest.mark.parametrize("run_type", AGENT_RUN_TYPES)
def test_every_run_type_maps_to_a_real_skill(run_type: str) -> None:
    skill, _ = REGISTRY[run_type]
    skill_md = SALES_SKILLS / skill / "SKILL.md"
    assert skill_md.is_file(), (
        f"run type {run_type!r} dispatches to skills/sales/{skill}/, which has no "
        "SKILL.md; `hermes --skills` would fail at run time"
    )


@pytest.mark.parametrize("run_type", AGENT_RUN_TYPES)
def test_every_run_type_carries_tenant_and_payload_into_the_prompt(run_type: str) -> None:
    """The skill arrives via `hermes --skills`; the prompt must carry the rest.

    HermesProcessExecutor runs `hermes -z <prompt> --skills <skill> --yolo`, so
    the skill name does not need to appear in the prompt text. What does need to
    survive the builder is the run's own inputs and the tenant it belongs to —
    a builder that drops the payload produces an agent run with nothing to act
    on, and one that drops the tenant invites cross-tenant work.
    """
    marker = "lead_bcd0f1a2"
    skill, prompt = build(run_type, "silverline", {"lead_ids": [marker]})
    assert skill == REGISTRY[run_type][0]
    assert prompt and prompt.strip(), f"{run_type} built an empty prompt"
    assert "silverline" in prompt, f"{run_type}'s prompt does not name the tenant"
    assert marker in prompt, (
        f"{run_type}'s builder dropped the run payload; the agent would start "
        "with no inputs"
    )


def test_analytics_refresh_stays_agentless() -> None:
    """It aggregates the database; giving it a skill would put an LLM in the path."""
    skill, prompt = build("analytics_refresh", "silverline", {})
    assert skill is None and prompt is None


def test_unknown_run_type_is_rejected_by_name() -> None:
    with pytest.raises(ValueError, match="unknown run_type"):
        build("lead_scan_v2", "silverline", {})


def test_read_only_and_send_types_are_disjoint_and_complete() -> None:
    """A send type contacts a prospect; a read-only one must not be able to."""
    assert not (READ_ONLY & SEND_TYPES), READ_ONLY & SEND_TYPES
    classified = READ_ONLY | SEND_TYPES | {"analytics_refresh"}
    assert classified == set(REGISTRY), (
        "every run type must be classified read-only or send; unclassified: "
        f"{set(REGISTRY) - classified}"
    )


@pytest.mark.parametrize(
    "skill_dir", sorted(p.parent for p in SALES_SKILLS.glob("*/SKILL.md")),
    ids=lambda p: p.name,
)
def test_sales_skill_frontmatter_is_usable(skill_dir: Path) -> None:
    meta = _frontmatter(skill_dir / "SKILL.md")
    assert meta.get("name") == skill_dir.name, (
        f"{skill_dir.name}/SKILL.md declares name={meta.get('name')!r}; the loader "
        "resolves skills by directory name, so they must match"
    )
    description = str(meta.get("description") or "")
    assert len(description) > 40, (
        f"{skill_dir.name} needs a description the agent can select on"
    )
    hermes = (meta.get("metadata") or {}).get("hermes") or {}
    assert hermes.get("category") == "sales", (
        f"{skill_dir.name} is under skills/sales/ but declares category="
        f"{hermes.get('category')!r}"
    )
