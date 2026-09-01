from agent.orchestration.task_context import TaskContext, sanitize_text, sanitize_value
from agent.orchestration.loop_guard import LoopGuard


def test_task_context_accepts_camel_and_snake_case_and_renders_markdown():
    ctx = TaskContext.from_mapping(
        {
            "IssueId": "#123",
            "issue_title": "Fix bug",
            "IssueDescription": "Beschreibung",
            "AcceptanceCriteria": ["A", "B"],
            "Repository": "/repo",
            "RelevantFiles": ["app.py"],
        }
    )

    rendered = ctx.render_markdown()

    assert ctx.issue_id == "#123"
    assert ctx.issue_title == "Fix bug"
    assert "## AcceptanceCriteria" in rendered
    assert "- A" in rendered
    assert "app.py" in rendered


def test_task_context_redacts_obvious_secret_lines():
    text = sanitize_text("normal\napi_key value\nbearer value\nnext")

    assert "normal" in text
    assert "next" in text
    assert "api_key value" not in text
    assert "bearer value" not in text
    assert text.count("[REDACTED sensitive line]") == 2


def test_task_context_sanitizes_metadata_recursively():
    ctx = TaskContext.from_mapping(
        {
            "issue_id": "#1",
            "metadata": {
                "api_key": "SECRET123",
                "nested": {"token": "TOK", "safe": "value"},
                "items": [{"password": "PW"}],
            },
        }
    )

    assert ctx.metadata["api_key"] == "[REDACTED]"
    assert ctx.metadata["nested"]["token"] == "[REDACTED]"
    assert ctx.metadata["nested"]["safe"] == "value"
    assert ctx.to_dict()["Metadata"]["items"][0]["password"] == "[REDACTED]"


def test_loop_guard_bounds_retries():
    guard = LoopGuard(max_correction_loops=2)

    assert guard.can_retry() is True
    assert guard.next_attempt() == 1
    assert guard.next_attempt() == 2
    assert guard.can_retry() is False
