"""CS-11a skill write-time routing-intent lint acceptance tests."""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli.skills import lint as lint_module
from hermes_cli.skills import lint_log, schema
from hermes_cli.skills.lint import Finding, lint_skill_body
from tools import skill_manager_tool


def _document(body: str, *, name: str = "lint-test") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: CS-11a test skill.\n"
        "---\n"
        f"{body}"
    )


def _categories(body: str) -> list[str]:
    return [finding.category for finding in lint_skill_body(body).findings]


@pytest.fixture(autouse=True)
def isolated_lint_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(schema, "DB_PATH", db_path)
    schema._MIGRATED_PATHS.discard(str(db_path.resolve()))
    return db_path


def test_empty_body_returns_no_findings():
    result = lint_skill_body("")
    assert result.linted_body == ""
    assert result.findings == []


def test_plain_body_returns_no_findings():
    body = "Call the customer, then update the sheet"
    result = lint_skill_body(body)
    assert result.linted_body == body
    assert result.findings == []


def test_model_slug_openai_stripped():
    result = lint_skill_body("use openai/gpt-5.6-sol for this step")
    assert "[STRIPPED: model_slug]" in result.linted_body
    assert result.findings[0].matched_text == "openai/gpt-5.6-sol"


def test_model_slug_openai_codex_stripped():
    assert _categories("Choose openai-codex.") == ["model_slug"]


def test_model_slug_anthropic_variants_stripped():
    body = "anthropic/claude-opus-5\nanthropic/claude-sonnet-5"
    assert _categories(body) == ["model_slug", "model_slug"]


def test_model_slug_glm_both_namespaces():
    body = "z-ai/glm-5.2\nzhipu/glm-5.2"
    assert _categories(body) == ["model_slug", "model_slug"]


def test_provider_directive_openrouter():
    result = lint_skill_body("route to openrouter")
    assert result.findings[0].category == "provider_directive"
    assert "openrouter" not in result.linted_body


def test_provider_directive_family():
    body = "use opus\nuse sonnet"
    assert _categories(body) == ["provider_directive", "provider_directive"]


def test_mode_directive_mode_colon():
    assert _categories("mode: moa") == ["mode_directive"]


def test_mode_directive_switch_to():
    assert _categories("switch to mixture-of-agents") == ["mode_directive"]


def test_mode_directive_rung_pin():
    assert _categories("rung: r4_opus5_single") == ["mode_directive"]


def test_cost_directive_spend_aud():
    assert _categories("spend up to AUD 3.00") == ["cost_directive"]


def test_cost_directive_escalate():
    assert _categories("escalate to opus") == ["cost_directive"]


def test_transport_directive_gateway():
    assert _categories("gateway: anthropic_direct") == ["transport_pin"]


def test_transport_directive_nitro():
    assert _categories(":nitro") == ["transport_pin"]


def test_case_insensitive():
    assert _categories("USE Openai/GPT-5.6-Sol") == ["model_slug"]


def test_pitfalls_note_appended_when_any_strip():
    result = lint_skill_body("# Steps\n\nuse openrouter")
    assert result.findings
    assert "## Pitfalls" in result.linted_body
    assert result.linted_body.count("**Lint note (CS-11a):**") == 1


def test_pitfalls_note_inserted_into_existing_pitfalls():
    body = "# Steps\n\nDo work.\n\n## Pitfalls\n\nKeep this warning.\n"
    result = lint_skill_body(body + "\nmode: moa\n")
    assert result.findings
    assert "Keep this warning." in result.linted_body
    assert result.linted_body.index("**Lint note (CS-11a):**") > result.linted_body.index("## Pitfalls")
    assert result.linted_body.count("## Pitfalls") == 1


def test_multiple_findings_same_body():
    body = "openai-codex\nmode: panel\nspend up to AUD 3.00\n"
    result = lint_skill_body(body)
    assert len(result.findings) == 3
    assert result.linted_body.count("**Lint note (CS-11a):**") == 1


def test_frontmatter_untouched(tmp_path: Path):
    prefix = (
        "---\r\nname: exact\r\ndescription: Keep bytes exactly.\r\n---\r\n"
    )
    target = tmp_path / "exact" / "SKILL.md"
    skill_manager_tool._write_skill_file(
        target,
        prefix + "Use openrouter.\r\n",
        skill_name="exact",
        write_source="test",
    )
    written = target.read_bytes()
    assert written.startswith(prefix.encode("utf-8"))


def test_lint_log_row_written_per_finding(
    tmp_path: Path,
    isolated_lint_db: Path,
):
    target = tmp_path / "logged" / "SKILL.md"
    skill_manager_tool._write_skill_file(
        target,
        _document(
            "openai-codex\nmode: panel\ntransport: openrouter\n",
            name="logged",
        ),
        skill_name="logged",
        write_source="edit",
    )
    with sqlite3.connect(isolated_lint_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM skill_lint_log").fetchone()[0] == 3


def test_lint_log_empty_when_clean_body(
    tmp_path: Path,
    isolated_lint_db: Path,
):
    schema.migrate(isolated_lint_db)
    target = tmp_path / "clean" / "SKILL.md"
    skill_manager_tool._write_skill_file(
        target,
        _document("# Steps\n\nDo the task.\n", name="clean"),
        skill_name="clean",
        write_source="create",
    )
    with sqlite3.connect(isolated_lint_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM skill_lint_log").fetchone()[0] == 0


def test_lint_log_ledger_migration_lazy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    fresh_db = tmp_path / "fresh-home" / "kanban.db"
    monkeypatch.setattr(schema, "DB_PATH", fresh_db)
    schema._MIGRATED_PATHS.discard(str(fresh_db.resolve()))
    lint_log.record_findings(
        skill_name="fresh",
        skill_path=tmp_path / "SKILL.md",
        write_source="create",
        findings=[
            Finding(
                "model_slug",
                "openai_codex",
                "openai-codex",
                1,
                "[STRIPPED: model_slug]",
            )
        ],
    )
    with sqlite3.connect(fresh_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM skill_lint_log").fetchone()[0] == 1


def test_lint_log_filter_by_skill_and_category(isolated_lint_db: Path):
    findings = [
        Finding("model_slug", "one", "openai-codex", 1, "[STRIPPED: model_slug]"),
        Finding("mode_directive", "two", "mode: moa", 2, "[STRIPPED: mode_directive]"),
    ]
    lint_log.record_findings(
        skill_name="alpha",
        skill_path="/tmp/alpha/SKILL.md",
        write_source="edit",
        findings=findings,
    )
    lint_log.record_findings(
        skill_name="beta",
        skill_path="/tmp/beta/SKILL.md",
        write_source="edit",
        findings=findings[:1],
    )
    rows = lint_log.list_findings(
        skill_name="alpha",
        category="mode_directive",
        db_path=isolated_lint_db,
    )
    assert len(rows) == 1
    assert rows[0]["skill_name"] == "alpha"
    assert rows[0]["category"] == "mode_directive"


def test_lint_log_write_failure_does_not_corrupt_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "non-gating" / "SKILL.md"

    def fail_record(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(lint_log, "record_lint", fail_record)
    skill_manager_tool._write_skill_file(
        target,
        _document("Use openrouter.\n", name="non-gating"),
        skill_name="non-gating",
        write_source="edit",
    )
    written = target.read_text(encoding="utf-8")
    assert "[STRIPPED: provider_directive]" in written
    assert "Use openrouter" not in written


def test_skill_manage_write_lints_and_writes_disk(
    tmp_path: Path,
    isolated_lint_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", skills_root)
    monkeypatch.setattr(
        "agent.skill_utils.get_all_skills_dirs",
        lambda: [skills_root],
    )
    monkeypatch.setattr(
        skill_manager_tool,
        "_apply_skill_write_gate",
        lambda *_args, **_kwargs: None,
    )
    raw = skill_manager_tool.skill_manage(
        action="create",
        name="actual",
        content=_document("Use openrouter.\n", name="actual"),
    )
    assert json.loads(raw)["success"] is True
    written = (skills_root / "actual" / "SKILL.md").read_text(encoding="utf-8")
    assert "[STRIPPED: provider_directive]" in written
    with sqlite3.connect(isolated_lint_db) as conn:
        row = conn.execute(
            "SELECT skill_name, write_source, category FROM skill_lint_log"
        ).fetchone()
    assert row == ("actual", "skill_manage", "provider_directive")


def test_skill_manage_write_atomic_on_lint_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "unchanged" / "SKILL.md"
    target.parent.mkdir()
    target.write_text("original bytes", encoding="utf-8")

    def fail_lint(_body):
        raise RuntimeError("lint failed")

    monkeypatch.setattr(lint_module, "lint_skill_body", fail_lint)
    with pytest.raises(RuntimeError, match="lint failed"):
        skill_manager_tool._write_skill_file(
            target,
            _document("Use openrouter.", name="unchanged"),
            skill_name="unchanged",
            write_source="edit",
        )
    assert target.read_text(encoding="utf-8") == "original bytes"


def test_no_bypass_flag_exists():
    for function in (
        skill_manager_tool._write_skill_file,
        skill_manager_tool._create_skill,
        skill_manager_tool._edit_skill,
        skill_manager_tool._patch_skill,
        skill_manager_tool._write_file,
        skill_manager_tool.skill_manage,
    ):
        parameters = inspect.signature(function).parameters
        assert "skip_lint" not in parameters
        assert "bypass_lint" not in parameters


def _hermes_executable() -> Path:
    return Path(__file__).parents[1] / ".venv" / "bin" / "hermes"


def _cli_env(hermes_home: Path) -> dict[str, str]:
    return {**os.environ, "HERMES_HOME": str(hermes_home)}


def test_cli_lint_check_prints_findings(tmp_path: Path):
    skill_path = tmp_path / "dirty-SKILL.md"
    skill_path.write_text(
        _document("openai-codex\nmode: moa\n"),
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(_hermes_executable()), "skills", "lint-check", str(skill_path)],
        check=True,
        capture_output=True,
        text=True,
        env=_cli_env(tmp_path / "home"),
    )
    assert "model_slug" in result.stdout
    assert "mode_directive" in result.stdout


def test_cli_lint_log_filters_work(tmp_path: Path):
    hermes_home = tmp_path / "cli-home"
    db_path = hermes_home / "kanban.db"
    schema.migrate(db_path)
    lint_log.record_lint(
        skill_name="seed",
        skill_path="/tmp/seed/SKILL.md",
        write_source="skill_manage",
        findings=[
            Finding(
                category,
                f"label-{index}",
                f"match-{index}",
                index,
                f"[STRIPPED: {category}]",
            )
            for index, category in enumerate(
                [
                    "model_slug",
                    "mode_directive",
                    "model_slug",
                    "cost_directive",
                    "transport_pin",
                ],
                start=1,
            )
        ],
        db_path=db_path,
    )
    result = subprocess.run(
        [
            str(_hermes_executable()),
            "skills",
            "lint-log",
            "--category",
            "model_slug",
            "--limit",
            "10",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_cli_env(hermes_home),
    )
    assert result.stdout.count("model_slug") == 2
    assert "mode_directive" not in result.stdout
    assert "cost_directive" not in result.stdout
    assert "transport_pin" not in result.stdout


def test_cli_lint_stats_counts_last_7_days(tmp_path: Path):
    hermes_home = tmp_path / "stats-home"
    db_path = hermes_home / "kanban.db"
    schema.migrate(db_path)
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    old = (now - timedelta(days=8)).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO skill_lint_log (
                ts, skill_name, skill_path, write_source, category,
                pattern_label, matched_text, line_number, replacement
            ) VALUES (?, 'stats', '/tmp/stats/SKILL.md', 'edit', ?, 'test',
                      'match', 1, 'replacement')
            """,
            [
                (recent, "transport_pin"),
                (old, "cost_directive"),
            ],
        )
    result = subprocess.run(
        [str(_hermes_executable()), "skills", "lint-stats"],
        check=True,
        capture_output=True,
        text=True,
        env=_cli_env(hermes_home),
    )
    assert "transport_pin: 1" in result.stdout
    assert "cost_directive" not in result.stdout
