"""Tests for the blueprints layer (skill frontmatter <-> cron automation bridge).

A blueprint is a skill with a metadata.hermes.blueprint block. These verify parsing,
the create-job bridge, and the export round-trip without touching the real
cron store.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.blueprints import (
    BlueprintError,
    BlueprintSpec,
    create_blueprint_job,
    export_blueprint,
    parse_blueprint,
    blueprint_spec_for_installed,
)


BLUEPRINT_SKILL = """---
name: morning-brief
description: Summarize unread email and calendar every morning.
version: 1.0.0
metadata:
  hermes:
    tags: [blueprint, email]
    blueprint:
      schedule: "0 8 * * *"
      deliver: telegram
      prompt: "Summarize my unread email and today's calendar."
---

# Morning Brief

Every morning, gather unread email and the day's calendar and send a digest.
"""

PLAIN_SKILL = """---
name: not-a-blueprint
description: Just a regular skill.
metadata:
  hermes:
    tags: [misc]
---

# Not a blueprint
"""

MALFORMED_BLUEPRINT = """---
name: broken
description: Blueprint with no schedule.
metadata:
  hermes:
    blueprint:
      deliver: origin
---

# Broken
"""


def _write_blueprint(
    root: Path,
    directory: str,
    *,
    name: str,
    schedule: str,
    body: str = "body",
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "metadata:\n"
        "  hermes:\n"
        "    blueprint:\n"
        f'      schedule: "{schedule}"\n'
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_authority_config(home: Path, roots: tuple[Path, ...]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    lines = ["skills:"]
    if roots:
        lines.append("  central_private_roots:")
        lines.extend(f"    - {root}" for root in roots)
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestParseBlueprint:
    def test_parses_full_blueprint(self):
        spec = parse_blueprint(BLUEPRINT_SKILL)
        assert spec is not None
        assert spec.skill_name == "morning-brief"
        assert spec.schedule == "0 8 * * *"
        assert spec.deliver == "telegram"
        assert spec.prompt is not None and spec.prompt.startswith("Summarize")


    def test_deliver_defaults_to_origin(self):
        skill = (
            "---\nname: r\ndescription: d\nmetadata:\n  hermes:\n"
            '    blueprint:\n      schedule: "every 1h"\n---\n\nbody'
        )
        spec = parse_blueprint(skill)
        assert spec is not None
        assert spec.deliver == "origin"


class TestBlueprintSpecForInstalled:
    def test_finds_and_parses_installed_blueprint(self, tmp_path):
        skills_dir = tmp_path / "skills"
        rec_dir = skills_dir / "productivity" / "morning-brief"
        rec_dir.mkdir(parents=True)
        (rec_dir / "SKILL.md").write_text(BLUEPRINT_SKILL, encoding="utf-8")

        with patch("tools.skills_hub.SKILLS_DIR", skills_dir):
            spec = blueprint_spec_for_installed("morning-brief")
        assert spec is not None
        assert spec.schedule == "0 8 * * *"


    def test_plain_skill_returns_none(self, tmp_path):
        skills_dir = tmp_path / "skills"
        d = skills_dir / "misc" / "not-a-blueprint"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(PLAIN_SKILL, encoding="utf-8")
        with patch("tools.skills_hub.SKILLS_DIR", skills_dir):
            assert blueprint_spec_for_installed("not-a-blueprint") is None


    @pytest.mark.parametrize("boundary", ["equal", "nested", "containing", "symlink_alias"])
    def test_mcp_authority_boundary_blocks_guessed_blueprint_names(
        self, tmp_path, monkeypatch, caplog, boundary
    ):
        from agent import skill_utils

        home = tmp_path / "home"
        skills_dir = home / "skills"
        private_body = "PRIVATE BLUEPRINT BODY 8675309"
        private = _write_blueprint(
            skills_dir,
            "private/canonical-private-workflow-8675309",
            name="canonical-private-workflow-8675309",
            schedule="0 1 * * *",
            body=private_body,
        )
        _write_blueprint(
            skills_dir,
            "adapter",
            name="adapter",
            schedule="0 2 * * *",
        )
        if boundary == "equal":
            authority = private
        elif boundary == "nested":
            authority = private.parent
        elif boundary == "containing":
            authority = skills_dir
        else:
            authority = tmp_path / "authority-alias"
            authority.symlink_to(private, target_is_directory=True)
        _write_authority_config(home, (authority,))
        monkeypatch.setenv("HERMES_HOME", str(home))
        skill_utils._external_dirs_cache_clear()

        with patch("tools.skills_hub.SKILLS_DIR", skills_dir):
            assert blueprint_spec_for_installed("canonical-private-workflow-8675309") is None
            if boundary != "containing":
                adapter = blueprint_spec_for_installed("adapter")
                assert adapter is not None
                assert adapter.schedule == "0 2 * * *"

        assert "canonical-private-workflow-8675309" not in caplog.text
        assert private_body not in caplog.text
        assert str(private) not in caplog.text


    def test_colliding_blueprint_directories_use_sorted_native_index_order(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _write_blueprint(skills_dir, "z-last/collision", name="collision", schedule="0 9 * * *")
        _write_blueprint(skills_dir, "a-first/collision", name="collision", schedule="0 7 * * *")

        with patch("tools.skills_hub.SKILLS_DIR", skills_dir):
            spec = blueprint_spec_for_installed("collision")

        assert spec is not None
        assert spec.schedule == "0 7 * * *"


    def test_authority_config_cache_tracks_edits_and_profile_switches(self, tmp_path, monkeypatch):
        from agent import skill_utils

        first_home = tmp_path / "first-home"
        skills_dir = first_home / "skills"
        private = _write_blueprint(
            skills_dir,
            "private/profile-private",
            name="profile-private",
            schedule="0 1 * * *",
        )
        _write_authority_config(first_home, (private,))
        monkeypatch.setenv("HERMES_HOME", str(first_home))
        skill_utils._external_dirs_cache_clear()

        with patch("tools.skills_hub.SKILLS_DIR", skills_dir):
            assert blueprint_spec_for_installed("profile-private") is None

            _write_authority_config(first_home, ())
            config = first_home / "config.yaml"
            stat = config.stat()
            os.utime(config, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
            visible = blueprint_spec_for_installed("profile-private")
            assert visible is not None

            second_home = tmp_path / "second-home"
            _write_authority_config(second_home, (private,))
            monkeypatch.setenv("HERMES_HOME", str(second_home))
            assert blueprint_spec_for_installed("profile-private") is None


class TestCreateBlueprintJob:
    def test_bridges_to_create_job(self):
        spec = parse_blueprint(BLUEPRINT_SKILL)
        assert spec is not None
        captured = {}

        def fake_create_job(**kwargs):
            captured.update(kwargs)
            return {"id": "abc123", **kwargs}

        with patch("cron.jobs.create_job", fake_create_job):
            job = create_blueprint_job(spec, origin={"platform": "telegram"})

        assert captured["schedule"] == "0 8 * * *"
        assert captured["skills"] == ["morning-brief"]
        assert captured["deliver"] == "telegram"
        assert captured["prompt"].startswith("Summarize")
        assert job["id"] == "abc123"


class TestExportBlueprint:
    def test_round_trips_job_to_skill_md(self):
        job = {
            "name": "My Morning Brief",
            "schedule_display": "0 8 * * *",
            "skills": ["morning-brief"],
            "deliver": "telegram",
            "prompt": "Summarize my unread email.",
        }
        md = export_blueprint(job, "# Morning Brief\n\nDoes the morning digest.")
        # The exported SKILL.md must itself parse back as a blueprint.
        spec = parse_blueprint(md)
        assert spec is not None
        assert spec.schedule == "0 8 * * *"
        assert spec.deliver == "telegram"
        # Name is sanitized to a valid skill identifier.
        assert spec.skill_name == "my-morning-brief"


    def test_export_interval_job_without_display(self):
        # Regression: parse_schedule stores interval periods as "minutes" —
        # exporting a job with only the parsed schedule dict must round-trip
        # the real interval, not fall back to the daily default.
        job = {
            "name": "poller",
            "schedule": {"kind": "interval", "minutes": 30},
            "skills": ["poller"],
        }
        md = export_blueprint(job, "body")
        spec = parse_blueprint(md)
        assert spec is not None
        assert spec.schedule == "every 30m"

        job["schedule"] = {"kind": "interval", "minutes": 120}
        spec = parse_blueprint(export_blueprint(job, "body"))
        assert spec is not None
        assert spec.schedule == "every 2h"
