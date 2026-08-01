"""Tests for the unified learning inbox adapter."""

import importlib

import pytest


@pytest.fixture
def inbox_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Both modules cache profile-rooted paths at import time in some test
    # environments. Reloading keeps this fixture isolated from the real profile.
    import hermes_constants
    importlib.reload(hermes_constants)
    import tools.write_approval as wa
    importlib.reload(wa)
    import cron.suggestions as suggestions
    importlib.reload(suggestions)
    yield home, wa, suggestions


def test_inbox_aggregates_existing_approval_stores(inbox_home):
    _, wa, suggestions = inbox_home
    wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "user", "content": "prefers concise UI"},
        summary="prefers concise UI",
        origin="background_review",
    )
    wa.stage_write(
        wa.SKILLS,
        {"action": "create", "name": "learning-inbox", "content": "---\nname: learning-inbox\n---\n"},
        summary="create learning-inbox",
        origin="background_review",
    )
    suggestions.add_suggestion(
        title="Daily review",
        description="Review learning candidates",
        source="usage",
        job_spec={"name": "Daily review", "prompt": "review", "schedule": "0 9 * * *"},
        dedup_key="usage:daily-review",
    )

    from agent import learning_inbox

    payload = learning_inbox.inbox_payload()
    assert payload["count"] == 3
    assert payload["counts"] == {"memory": 1, "skill": 1, "automation": 1}
    assert {item["kind"] for item in payload["items"]} == {"memory", "skill", "automation"}
    assert payload["settings"] == {
        "memory_write_approval": False,
        "skills_write_approval": False,
    }


def test_detail_is_namespaced_and_contains_reviewable_proposal(inbox_home):
    _, wa, _ = inbox_home
    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "use local-first storage"},
        summary="use local-first storage",
        origin="foreground",
    )

    from agent.learning_inbox import get_item

    item = get_item("memory", record["id"])
    assert item is not None
    assert item["id"] == f"memory:{record['id']}"
    assert "use local-first storage" in item["detail"]
    assert item["evidence"]["origin"] == "foreground"


def test_approve_memory_reuses_existing_replay_path(inbox_home):
    _, wa, _ = inbox_home
    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "user", "content": "prefers evidence-backed work"},
        summary="prefers evidence-backed work",
        origin="background_review",
    )

    from agent.learning_inbox import approve
    from tools.memory_tool import MemoryStore

    result = approve("memory", record["id"])
    assert result["ok"] is True
    assert wa.get_pending(wa.MEMORY, record["id"]) is None

    store = MemoryStore()
    store.load_from_disk()
    assert "prefers evidence-backed work" in store.user_entries


def test_approve_automation_reuses_cron_suggestion_acceptance(inbox_home, monkeypatch):
    _, _, suggestions = inbox_home
    record = suggestions.add_suggestion(
        title="Daily review",
        description="Review learning candidates",
        source="usage",
        job_spec={"name": "Daily review", "prompt": "review", "schedule": "0 9 * * *"},
        dedup_key="usage:daily-review",
    )

    created = {}

    def fake_create_job(**kwargs):
        created.update(kwargs)
        return {"id": "job-1", **kwargs}

    monkeypatch.setattr("cron.jobs.create_job", fake_create_job)
    from agent.learning_inbox import approve

    result = approve("automation", record["id"])
    assert result["ok"] is True
    assert created["name"] == "Daily review"
    assert suggestions.list_pending() == []


def test_invalid_reference_is_rejected(inbox_home):
    from agent.learning_inbox import get_item

    with pytest.raises(ValueError):
        get_item("memory", "../secrets")
