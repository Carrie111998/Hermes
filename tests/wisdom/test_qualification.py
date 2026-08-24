from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_wisdom.qualification import (
    HIGH_USAGE_CONSECUTIVE_DAYS,
    RETENTION_DAYS,
    _emit_candidate,
    record_mutation,
    record_successful_use,
)
from hermes_wisdom.store import WisdomStore


def _configured_store(tmp_path: Path) -> WisdomStore:
    store = WisdomStore(tmp_path / "state")
    store.installation_identity()
    store.verify_installation_identity("org-1")
    return store


def _skill(tmp_path: Path) -> Path:
    path = tmp_path / "skills" / "learned-skill"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        "---\nname: learned-skill\n---\n# One\n", encoding="utf-8"
    )
    return path


def _eligible(monkeypatch, skill: Path) -> None:
    monkeypatch.setattr(
        "hermes_wisdom.qualification.get_skills_dir", lambda: skill.parent
    )
    monkeypatch.setattr(
        "hermes_wisdom.qualification._find_skill_dir", lambda _name: skill
    )
    monkeypatch.setattr("hermes_wisdom.qualification.is_bundled", lambda _name: False)
    monkeypatch.setattr(
        "hermes_wisdom.qualification.is_hub_installed", lambda _name: False
    )


def test_high_usage_threshold_uses_consecutive_utc_days_and_deduplicates(
    monkeypatch, tmp_path: Path
):
    skill = _skill(tmp_path)
    _eligible(monkeypatch, skill)
    store = _configured_store(tmp_path)
    start = datetime(2026, 8, 1, 23, 55, tzinfo=timezone.utc)

    for offset in range(HIGH_USAGE_CONSECUTIVE_DAYS - 1):
        assert (
            record_successful_use(
                "learned-skill", at=start + timedelta(days=offset), store=store
            )
            is None
        )
    event_id = record_successful_use(
        "learned-skill",
        at=start + timedelta(days=HIGH_USAGE_CONSECUTIVE_DAYS - 1),
        session_id="session-1",
        task_id="task-1",
        store=store,
    )
    assert event_id
    assert (
        record_successful_use(
            "learned-skill",
            at=start + timedelta(days=HIGH_USAGE_CONSECUTIVE_DAYS),
            store=store,
        )
        is None
    )
    events = store.local_events(kind="wisdom.candidate")
    assert len(events) == 1
    assert events[0]["session_id"] == "session-1"
    assert events[0]["payload"]["networked"] is False


def test_usage_retention_is_bounded(monkeypatch, tmp_path: Path):
    skill = _skill(tmp_path)
    _eligible(monkeypatch, skill)
    store = _configured_store(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for offset in range(RETENTION_DAYS + 8):
        record_successful_use(
            "learned-skill", at=start + timedelta(days=offset * 2), store=store
        )
    with store.transaction() as db:
        rows = db.execute("SELECT day_utc FROM usage_day ORDER BY day_utc").fetchall()
    assert len(rows) <= (RETENTION_DAYS + 1) // 2
    assert rows[0][0] >= (start.date() + timedelta(days=50)).isoformat()


def test_structural_refinements_schedule_restart_safe_stability(
    monkeypatch, tmp_path: Path
):
    skill = _skill(tmp_path)
    _eligible(monkeypatch, skill)
    store = _configured_store(tmp_path)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    record_successful_use("learned-skill", at=start, store=store)

    for index in range(3):
        refs = skill / "refs"
        refs.mkdir(exist_ok=True)
        (refs / f"decision-{index}.md").write_text(
            f"decision {index}", encoding="utf-8"
        )
        record_mutation(
            "learned-skill", at=start + timedelta(days=index + 1), store=store
        )

    restarted = WisdomStore(store.root)
    event_id = record_successful_use(
        "learned-skill",
        at=start + timedelta(days=10),
        session_id="session-stable",
        store=restarted,
    )
    assert event_id
    event = restarted.local_events(kind="wisdom.candidate")[0]
    assert event["qualification"] == "refinement"
    assert event["payload"]["local_reasons"]["meaningful_refinements"] == 3


def test_ambiguous_classifier_failure_is_conservative(monkeypatch, tmp_path: Path):
    skill = _skill(tmp_path)
    _eligible(monkeypatch, skill)
    store = _configured_store(tmp_path)
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    record_successful_use("learned-skill", at=now, store=store)
    (skill / "SKILL.md").write_text(
        "---\nname: learned-skill\n---\n# Typo only\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "hermes_wisdom.qualification._classify_ambiguous",
        lambda *_args: "non_meaningful",
    )
    record_mutation("learned-skill", at=now + timedelta(days=1), store=store)
    with store.transaction() as db:
        row = db.execute("SELECT classification FROM refinement").fetchone()
    assert row[0] == "non_meaningful"
    assert store.due_stability_jobs((now + timedelta(days=30)).isoformat()) == []


def test_dismissal_suppresses_exact_content_but_stronger_path_can_resuggest(
    tmp_path: Path,
):
    store = _configured_store(tmp_path)
    skill = _skill(tmp_path)
    skill_id = store.register_skill(
        skill, content_hash="sha256:one", source_kind="local"
    )
    first = _emit_candidate(
        store,
        skill_id=skill_id,
        skill_name="learned-skill",
        content_hash="sha256:one",
        qualification="refinement",
        local_reasons={},
        session_id=None,
        task_id=None,
    )
    stronger = _emit_candidate(
        store,
        skill_id=skill_id,
        skill_name="learned-skill",
        content_hash="sha256:one",
        qualification="high_usage",
        local_reasons={},
        session_id=None,
        task_id=None,
    )
    assert first and stronger
    store.dismiss_candidate(skill_id, "sha256:one")
    assert store.local_events(kind="wisdom.candidate") == []
    assert (
        _emit_candidate(
            store,
            skill_id=skill_id,
            skill_name="learned-skill",
            content_hash="sha256:one",
            qualification="high_usage",
            local_reasons={},
            session_id=None,
            task_id=None,
        )
        is None
    )
    assert _emit_candidate(
        store,
        skill_id=skill_id,
        skill_name="learned-skill",
        content_hash="sha256:two",
        qualification="high_usage",
        local_reasons={},
        session_id=None,
        task_id=None,
    )
