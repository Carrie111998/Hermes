import json

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import autonomy_inventory
from hermes_cli.autonomy_inventory import build_inventory, inventory_mcp, inventory_skills


def test_inventory_skills_reports_frontmatter_without_reading_other_files(tmp_path):
    skill_dir = tmp_path / "skills" / "research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: research
description: Test hypotheses.
---
# Objective
# Preconditions
# Procedure
# Error handling
# Success criteria
# Tests
""",
        encoding="utf-8",
    )

    rows = inventory_skills(tmp_path / "skills")

    assert rows == [
        {
            "name": "research",
            "path": str(skill_dir / "SKILL.md"),
            "yaml_valid": True,
            "valid": True,
            "recommended_sections_present": [
                "objective",
                "preconditions",
                "procedure",
                "error handling",
                "success criteria",
                "tests",
            ],
            "issues": [],
        }
    ]


def test_inventory_mcp_redacts_connection_material_and_keeps_only_key_names():
    rows = inventory_mcp(
        {
            "mcp_servers": {
                "trader": {
                    "url": "https://alice:hunter2@example.test/mcp",
                    "headers": {
                        "Authorization": "Bearer ${TRADER_TOKEN}",
                        "X-Unusual": "hunter2",
                    },
                    "env": {"UNUSUAL_NAME": "hunter2"},
                    "command": "server --credential hunter2",
                    "args": ["--password", "hunter2"],
                    "tools": {"include": ["read_market"]},
                }
            }
        }
    )

    dumped = json.dumps(rows)
    assert rows[0]["env_refs"] == ["TRADER_TOKEN"]
    assert rows[0]["env_keys"] == ["UNUSUAL_NAME"]
    assert rows[0]["header_keys"] == ["Authorization", "X-Unusual"]
    assert rows[0]["tool_allowlist"] == ["read_market"]
    assert "hunter2" not in dumped
    assert "alice" not in dumped
    assert "Bearer " not in dumped
    assert "TRADER_TOKEN" in dumped


def test_build_inventory_never_reads_environment_values(tmp_path, monkeypatch):
    token = set_hermes_home_override(tmp_path)
    try:
        (tmp_path / "skills").mkdir()
        (tmp_path / ".env").write_text(
            "OPENAI_API_KEY=sk-live-secret\nNORMAL_SETTING=visible-value\n",
            encoding="utf-8",
        )
        (tmp_path / "config.yaml").write_text(
            """
mcp_servers:
  trader:
    url: https://alice:secret@example.test/mcp
    headers:
      Authorization: Bearer secret
approvals:
  token: approval-secret
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            autonomy_inventory,
            "inventory_tools",
            lambda: {"imported_modules": [], "tool_count": 0, "toolsets": {}, "aliases": {}},
        )

        report = build_inventory()
    finally:
        reset_hermes_home_override(token)

    dumped = json.dumps(report)
    assert report["secrets"]["env_keys"] == ["NORMAL_SETTING", "OPENAI_API_KEY"]
    assert "sk-live-secret" not in dumped
    assert "visible-value" not in dumped
    assert "approval-secret" not in dumped
    assert "alice" not in dumped
