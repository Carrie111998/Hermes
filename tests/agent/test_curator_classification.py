"""Model-sovereign curator classification and reporting contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def curator_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    (home / "logs").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    import importlib
    import hermes_constants

    importlib.reload(hermes_constants)
    from agent import curator

    importlib.reload(curator)
    yield curator


def _delete(name: str, absorbed_into: str) -> dict:
    return {
        "name": "skill_manage",
        "arguments": json.dumps({
            "action": "delete",
            "name": name,
            "absorbed_into": absorbed_into,
        }),
    }


def test_removed_skill_projection_uses_explicit_delete_declarations(curator_env):
    result = curator_env._classify_removed_skills(
        removed=["narrow", "stale"],
        added=["umbrella"],
        after_names={"umbrella"},
        tool_calls=[_delete("narrow", "umbrella"), _delete("stale", "")],
    )

    assert result["consolidated"] == [
        {
            "name": "narrow",
            "into": "umbrella",
            "source": "absorbed_into (model-declared at delete)",
        }
    ]
    assert result["pruned"][0]["name"] == "stale"
    assert result["unclassified"] == []


@pytest.mark.parametrize(
    "authored_text",
    [
        "narrow",
        "references/narrow.md",
        "Absorbed narrow into umbrella",
        "error: narrow failed then recovered",
        "running latest tests with pytest",
    ],
)
def test_arbitrary_skill_text_never_classifies_removal(curator_env, authored_text):
    result = curator_env._classify_removed_skills(
        removed=["narrow"],
        added=["umbrella"],
        after_names={"umbrella"},
        tool_calls=[
            {
                "name": "skill_manage",
                "arguments": json.dumps({
                    "action": "write_file",
                    "name": "umbrella",
                    "file_path": authored_text,
                    "file_content": authored_text,
                    "content": authored_text,
                }),
            }
        ],
    )

    assert result["consolidated"] == []
    assert result["pruned"] == []
    assert result["unclassified"] == [
        {"name": "narrow", "source": "missing_model_delete_declaration"}
    ]


def test_invalid_declared_destination_is_unclassified(curator_env):
    result = curator_env._classify_removed_skills(
        removed=["narrow"],
        added=[],
        after_names={"real-umbrella"},
        tool_calls=[_delete("narrow", "ghost-umbrella")],
    )

    assert result["consolidated"] == []
    assert result["pruned"] == []
    assert result["unclassified"][0]["model_claimed_into"] == "ghost-umbrella"


def test_parse_structured_summary_happy_path(curator_env):
    parsed = curator_env._parse_structured_summary(
        """Done.\n```yaml
consolidations:
  - from: narrow
    into: umbrella
    reason: duplicate
prunings:
  - name: stale
    reason: obsolete
```"""
    )

    assert parsed["consolidations"] == [
        {"from": "narrow", "into": "umbrella", "reason": "duplicate"}
    ]
    assert parsed["prunings"] == [{"name": "stale", "reason": "obsolete"}]


@pytest.mark.parametrize("text", ["", "no block", "```yaml\nnot: [valid\n```"])
def test_parse_structured_summary_malformed_or_absent_is_empty(curator_env, text):
    assert curator_env._parse_structured_summary(text) == {
        "consolidations": [],
        "prunings": [],
    }


def test_extract_delete_declarations_is_schema_driven(curator_env):
    declarations = curator_env._extract_absorbed_into_declarations([
        _delete("narrow", "umbrella"),
        _delete("stale", ""),
        {
            "name": "skill_manage",
            "arguments": json.dumps({
                "action": "patch",
                "name": "other",
                "absorbed_into": "must-not-count",
            }),
        },
        {"name": "skill_manage", "arguments": "{bad json"},
    ])

    assert declarations == {
        "narrow": {"into": "umbrella", "declared": True},
        "stale": {"into": "", "declared": True},
    }


def test_reconcile_uses_model_authored_structured_block(curator_env):
    result = curator_env._reconcile_classification(
        removed=["narrow", "stale", "unknown"],
        model_block={
            "consolidations": [
                {"from": "narrow", "into": "umbrella", "reason": "duplicate"}
            ],
            "prunings": [{"name": "stale", "reason": "obsolete"}],
        },
        destinations={"umbrella"},
    )

    assert [item["name"] for item in result["consolidated"]] == ["narrow"]
    assert [item["name"] for item in result["pruned"]] == ["stale"]
    assert result["unclassified"] == [
        {"name": "unknown", "source": "missing_model_authored_classification"}
    ]


def test_delete_declaration_is_authoritative_and_preserves_model_reason(curator_env):
    result = curator_env._reconcile_classification(
        removed=["narrow", "stale"],
        model_block={
            "consolidations": [
                {"from": "narrow", "into": "umbrella", "reason": "duplicate"}
            ],
            "prunings": [{"name": "stale", "reason": "obsolete"}],
        },
        destinations={"umbrella"},
        absorbed_declarations={
            "narrow": {"into": "umbrella", "declared": True},
            "stale": {"into": "", "declared": True},
        },
    )

    assert result["consolidated"][0]["reason"] == "duplicate"
    assert result["pruned"][0]["reason"] == "obsolete"
    assert result["unclassified"] == []


def test_invalid_model_destination_remains_unclassified(curator_env):
    result = curator_env._reconcile_classification(
        removed=["narrow"],
        model_block={"consolidations": [], "prunings": []},
        destinations={"real-umbrella"},
        absorbed_declarations={"narrow": {"into": "ghost-umbrella", "declared": True}},
    )

    assert result["consolidated"] == []
    assert result["pruned"] == []
    assert result["unclassified"][0]["source"] == "invalid_model_delete_destination"


def test_report_splits_explicit_model_decisions(curator_env):
    before = [
        {"name": "narrow", "state": "active", "pinned": False},
        {"name": "stale", "state": "stale", "pinned": False},
    ]
    after = [{"name": "umbrella", "state": "active", "pinned": False}]
    final = """Done.\n```yaml
consolidations:
  - from: narrow
    into: umbrella
    reason: duplicate
prunings:
  - name: stale
    reason: obsolete
```"""

    run_dir = curator_env._write_run_report(
        started_at=datetime.now(timezone.utc),
        elapsed_seconds=1,
        auto_counts={"checked": 2, "marked_stale": 0, "archived": 0, "reactivated": 0},
        auto_summary="none",
        before_report=before,
        before_names={"narrow", "stale"},
        after_report=after,
        llm_meta={
            "final": final,
            "summary": "done",
            "model": "m",
            "provider": "p",
            "error": None,
            "tool_calls": [_delete("narrow", "umbrella"), _delete("stale", "")],
        },
    )

    payload = json.loads((run_dir / "run.json").read_text())
    assert [item["name"] for item in payload["consolidated"]] == ["narrow"]
    assert [item["name"] for item in payload["pruned"]] == ["stale"]
    assert payload["unclassified"] == []
    report = (run_dir / "REPORT.md").read_text()
    assert "Consolidated into umbrella skills" in report
    assert "Pruned — archived for staleness" in report


def test_report_exposes_missing_model_classification_without_guessing(curator_env):
    run_dir = curator_env._write_run_report(
        started_at=datetime.now(timezone.utc),
        elapsed_seconds=1,
        auto_counts={"checked": 1, "marked_stale": 0, "archived": 0, "reactivated": 0},
        auto_summary="none",
        before_report=[{"name": "narrow", "state": "active", "pinned": False}],
        before_names={"narrow"},
        after_report=[{"name": "umbrella", "state": "active", "pinned": False}],
        llm_meta={
            "final": "The authored prose says narrow was absorbed into umbrella.",
            "summary": "done",
            "model": "m",
            "provider": "p",
            "error": None,
            "tool_calls": [
                {
                    "name": "skill_manage",
                    "arguments": json.dumps({
                        "action": "write_file",
                        "name": "umbrella",
                        "content": "narrow was absorbed into umbrella",
                    }),
                }
            ],
        },
    )

    payload = json.loads((run_dir / "run.json").read_text())
    assert payload["consolidated"] == []
    assert payload["pruned"] == []
    assert payload["unclassified"][0]["name"] == "narrow"
    report = (run_dir / "REPORT.md").read_text()
    assert "model classification unavailable" in report
    assert "No keyword or content heuristic was used" in report


def test_rename_summary_uses_only_explicit_model_decisions(curator_env):
    summary = curator_env._build_rename_summary(
        before_names={"narrow", "stale"},
        after_report=[{"name": "umbrella", "state": "active"}],
        tool_calls=[_delete("narrow", "umbrella"), _delete("stale", "")],
        model_final="",
    )

    assert "narrow → umbrella" in summary
    assert "stale — pruned" in summary


def test_rename_summary_marks_opaque_prose_unclassified(curator_env):
    summary = curator_env._build_rename_summary(
        before_names={"narrow"},
        after_report=[{"name": "umbrella", "state": "active"}],
        tool_calls=[
            {
                "name": "skill_manage",
                "arguments": json.dumps({
                    "action": "patch",
                    "name": "umbrella",
                    "new_string": "narrow was consolidated here",
                }),
            }
        ],
        model_final="narrow was consolidated into umbrella",
    )

    assert "archived (model classification unavailable)" in summary
    assert "narrow → umbrella" not in summary


def test_parse_structured_summary_missing_block(curator_env):
    out = curator_env._parse_structured_summary("No block in this text.")
    assert out == {"consolidations": [], "prunings": []}


def test_reconcile_model_block_visible_in_full_report(curator_env):
    """End-to-end: LLM final response with the YAML block → reasons in REPORT.md."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    start = _dt.now(_tz.utc)
    before = [
        {"name": "anthropic-api", "state": "active", "pinned": False},
        {"name": "stale-thing", "state": "stale", "pinned": False},
    ]
    after = [{"name": "llm-providers", "state": "active", "pinned": False}]

    llm_final_text = (
        "Processed 3 clusters. Absorbed anthropic-api into llm-providers.\n\n"
        "## Structured summary (required)\n"
        "```yaml\n"
        "consolidations:\n"
        "  - from: anthropic-api\n"
        "    into: llm-providers\n"
        "    reason: duplicate content, now a subsection\n"
        "prunings:\n"
        "  - name: stale-thing\n"
        "    reason: pre-curator junk, no overlap with anything\n"
        "```\n"
    )

    run_dir = curator_env._write_run_report(
        started_at=start,
        elapsed_seconds=30.0,
        auto_counts={"checked": 2, "marked_stale": 0, "archived": 0, "reactivated": 0},
        auto_summary="none",
        before_report=before,
        before_names={r["name"] for r in before},
        after_report=after,
        llm_meta={
            "final": llm_final_text,
            "summary": "1 consolidated, 1 pruned",
            "model": "m",
            "provider": "p",
            "error": None,
            "tool_calls": [
                {
                    "name": "skill_manage",
                    "arguments": _json.dumps({
                        "action": "create",
                        "name": "llm-providers",
                        "content": "# llm-providers\nIncludes anthropic-api",
                    }),
                },
            ],
        },
    )

    payload = _json.loads((run_dir / "run.json").read_text())
    cons = payload["consolidated"][0]
    assert cons["name"] == "anthropic-api"
    assert cons["into"] == "llm-providers"
    assert cons["reason"] == "duplicate content, now a subsection"
    assert cons["source"] == "model_structured_summary"

    pruned = payload["pruned"][0]
    assert pruned["name"] == "stale-thing"
    assert pruned["reason"] == "pre-curator junk, no overlap with anything"

    md = (run_dir / "REPORT.md").read_text()
    assert "duplicate content, now a subsection" in md
    assert "pre-curator junk" in md


def test_extract_absorbed_into_picks_up_consolidation(curator_env):
    """Delete call with absorbed_into=<umbrella> yields a declaration."""
    declarations = curator_env._extract_absorbed_into_declarations([
        {
            "name": "skill_manage",
            "arguments": json.dumps({
                "action": "delete",
                "name": "narrow-skill",
                "absorbed_into": "umbrella",
            }),
        },
    ])
    assert declarations == {
        "narrow-skill": {"into": "umbrella", "declared": True},
    }


def test_extract_absorbed_into_ignores_non_delete_actions(curator_env):
    """Patch, create, write_file etc. must not leak into declarations."""
    declarations = curator_env._extract_absorbed_into_declarations([
        {
            "name": "skill_manage",
            "arguments": json.dumps({
                "action": "patch",
                "name": "umbrella",
                "old_string": "...",
                "new_string": "...",
                "absorbed_into": "something",  # bogus on non-delete, must be ignored
            }),
        },
    ])
    assert declarations == {}


def test_reconcile_absorbed_into_beats_everything_else(curator_env):
    """Model declared absorbed_into at delete; the structured declaration wins.

    This is the exact #18671 regression: the model forgets to emit the YAML
    summary block, there is no structured summary. The delete declaration remains the
    authoritative model-authored signal.
    """
    out = curator_env._reconcile_classification(
        removed=["pr-review-format"],
        model_block={"consolidations": [], "prunings": []},  # model forgot YAML block
        destinations={"hermes-agent-dev"},
        absorbed_declarations={
            "pr-review-format": {"into": "hermes-agent-dev", "declared": True},
        },
    )
    assert len(out["consolidated"]) == 1
    assert out["pruned"] == []
    e = out["consolidated"][0]
    assert e["name"] == "pr-review-format"
    assert e["into"] == "hermes-agent-dev"
    assert "absorbed_into" in e["source"]


def test_rename_summary_empty_when_nothing_archived(curator_env):
    """No removals = empty string (no log noise on no-op ticks)."""
    result = curator_env._build_rename_summary(
        before_names={"alpha", "beta"},
        after_report=[
            {"name": "alpha", "state": "active"},
            {"name": "beta", "state": "active"},
        ],
        tool_calls=[],
        model_final="",
    )
    assert result == ""


def test_rename_summary_pruned_marked_explicitly(curator_env):
    """Pruned skills (no umbrella) say `pruned (stale)` so users don't think they were merged."""
    result = curator_env._build_rename_summary(
        before_names={"old-flaky-thing", "keeper"},
        after_report=[{"name": "keeper", "state": "active"}],
        tool_calls=[
            {
                "name": "skill_manage",
                "arguments": json.dumps({
                    "action": "delete",
                    "name": "old-flaky-thing",
                    "absorbed_into": "",
                }),
            },
        ],
        model_final="",
    )
    assert "old-flaky-thing — pruned (stale)" in result
    assert "→" not in result.split("old-flaky-thing")[1].splitlines()[0]


def test_rename_summary_caps_at_ten_with_more_indicator(curator_env):
    """Large consolidations don't blow up the log line — cap + `… and N more`."""
    removed = [f"skill-{i}" for i in range(15)]
    tool_calls = [
        {
            "name": "skill_manage",
            "arguments": json.dumps({
                "action": "delete",
                "name": name,
                "absorbed_into": "umbrella",
            }),
        }
        for name in removed
    ]
    result = curator_env._build_rename_summary(
        before_names=set(removed) | {"umbrella"},
        after_report=[{"name": "umbrella", "state": "active"}],
        tool_calls=tool_calls,
        model_final="",
    )
    assert "archived 15 skill(s):" in result
    assert "… and 5 more" in result
    # Exactly 10 bullets shown
    bullet_count = sum(1 for ln in result.splitlines() if ln.startswith("  • "))
    assert bullet_count == 10


def test_rename_summary_mixed_consolidation_and_pruning(curator_env):
    """Consolidated entries come first, pruned entries follow — matches REPORT.md ordering."""
    result = curator_env._build_rename_summary(
        before_names={"merge-me", "drop-me", "umbrella"},
        after_report=[{"name": "umbrella", "state": "active"}],
        tool_calls=[
            {
                "name": "skill_manage",
                "arguments": json.dumps({
                    "action": "delete",
                    "name": "merge-me",
                    "absorbed_into": "umbrella",
                }),
            },
            {
                "name": "skill_manage",
                "arguments": json.dumps({
                    "action": "delete",
                    "name": "drop-me",
                    "absorbed_into": "",
                }),
            },
        ],
        model_final="",
    )
    lines = result.splitlines()
    merge_idx = next(i for i, ln in enumerate(lines) if "merge-me" in ln)
    drop_idx = next(i for i, ln in enumerate(lines) if "drop-me" in ln)
    assert merge_idx < drop_idx, "consolidated should render before pruned"
    assert "merge-me → umbrella" in lines[merge_idx]
    assert "drop-me — pruned (stale)" in lines[drop_idx]
