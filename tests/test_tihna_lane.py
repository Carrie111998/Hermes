from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from hermes_cli.lanes.contracts import (
    ApprovalGrant,
    ApprovalRequest,
    LaneDraft,
    LaneTask,
    PublishResult,
)
from hermes_cli.lanes.errors import PublishDisabled
from hermes_cli.lanes.impls.tihna import TihnaLane
from hermes_cli.lanes.registry import LaneRegistry

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
DIGEST = """# Tihna Weekly Trends — 2026-W30

## Signal Summary
Synthetic summary.

## Notable Papers
- Paper

## Community Chatter
- Community

## Adjacent Tech
- Tech

## Recommended Follow-ups
1. Read
"""


def _entry(
    external_id="entry-1",
    *,
    summary="x" * 150,
    pub_date="2026-07-25T10:00:00Z",
):
    return {
        "external_id": external_id,
        "feed_url": "https://example.test/feed",
        "title": "Synthetic item",
        "link": f"https://example.test/{external_id}",
        "pub_date": pub_date,
        "summary": summary,
        "author": "Author",
        "tags": [],
        "category": "papers",
    }


class FakeHarness:
    def __init__(self, *, publish_enabled=False, llm_text=None, dry_run=False):
        self.publish_enabled = publish_enabled
        self.llm_text = llm_text
        self.dry_run = dry_run
        self.tasks = []
        self.rate_calls = []
        self.metrics = []
        self.llm_calls = []
        self.lint_calls = []
        self.approval_drafts = []
        self.publish_calls = []

    def find_task(self, *, external_id):
        return next(
            (task for task in self.tasks if task.external_id == external_id),
            None,
        )

    def persist_task(self, task):
        if self.dry_run:
            return task
        persisted = LaneTask(
            lane_id=task.lane_id,
            external_id=task.external_id,
            payload=dict(task.payload),
            id=len(self.tasks) + 1,
            task_id=task.task_id,
            status=task.status,
        )
        self.tasks.append(persisted)
        return persisted

    def list_tasks(self, *, status=None, ingested_since=None):
        del ingested_since
        return [
            task
            for task in self.tasks
            if status is None or task.status == status
        ]

    def update_task(self, *, task, payload=None, status=None):
        updated = LaneTask(
            lane_id=task.lane_id,
            external_id=task.external_id,
            payload=payload if payload is not None else task.payload,
            id=task.id,
            task_id=task.task_id,
            status=status if status is not None else task.status,
        )
        self.tasks = [
            updated if existing.id == task.id else existing
            for existing in self.tasks
        ]
        return updated

    def check_rate_limit(self, **kwargs):
        self.rate_calls.append(kwargs)

    def record_metric(self, *, task, metric_name, value):
        self.metrics.append((task.id, metric_name, value))

    def call_llm(self, **kwargs):
        self.llm_calls.append(kwargs)
        if self.llm_text is not None:
            text = self.llm_text
        elif kwargs["purpose"] == "classification":
            text = (
                '[{"external_id":"entry-1","score":75,'
                '"reason":"strong signal"}]'
            )
        else:
            text = DIGEST
        return type("Result", (), {"text": text})()

    def lint_draft(self, text):
        self.lint_calls.append(text)
        return text

    def enqueue_approval(self, *, task, draft):
        self.approval_drafts.append(draft)
        return ApprovalRequest(
            token="Token1234567",
            lane_task_id=int(task.id or 0),
            status="pending",
            expires_at="2026-08-02T12:00:00Z",
        )

    def publish_with_ledger(self, **kwargs):
        self.publish_calls.append(kwargs)
        if not self.publish_enabled:
            raise PublishDisabled("publishing is disabled: tihna")
        external_ref = kwargs["publisher"](kwargs["payload"])
        return PublishResult(
            outcome="success",
            log_id=1,
            side_effect_id=2,
        ), external_ref


def _feed_file(tmp_path: Path) -> Path:
    path = tmp_path / "feeds.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "feeds": [
                    {
                        "name": "Fixture",
                        "url": "https://example.test/feed",
                        "category": "papers",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _lane(tmp_path: Path) -> TihnaLane:
    return TihnaLane(
        feeds_path=_feed_file(tmp_path),
        output_root=tmp_path / "digests",
        now_fn=lambda: NOW,
    )


def _seed_item(harness: FakeHarness, *, score=None, external_id="entry-1"):
    payload = {**_entry(external_id), "stage": "item"}
    if score is not None:
        payload["score"] = score
    return harness.persist_task(
        LaneTask(
            lane_id="tihna",
            external_id=external_id,
            task_id=f"tihna-{external_id}",
            payload=payload,
        )
    )


def _control(stage: str, task_id=100) -> LaneTask:
    return LaneTask(
        lane_id="tihna",
        external_id=f"{stage}-control",
        task_id=f"tihna-{stage}",
        id=task_id,
        payload={"stage": stage},
    )


def test_lane_object_registers_with_registry(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "lanes": [
                    {
                        "lane_id": "tihna",
                        "enabled": True,
                        "module": "hermes_cli.lanes.impls.tihna",
                        "approval_channel": "dashboard",
                        "approval_timeout_hours": 168,
                        "per_lane_daily_cost_cap_aud": 2.0,
                        "per_lane_daily_task_cap": 15,
                        "per_lane_hourly_ingest_cap": 5,
                        "publish_enabled": False,
                        "description": "test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = LaneRegistry(
        manifest_path=manifest,
        db_path=tmp_path / "db.sqlite",
    )
    assert isinstance(registry.activate("tihna"), TihnaLane)


def test_lane_object_matches_BusinessLane_protocol(tmp_path):
    lane = _lane(tmp_path)
    for method in ("ingest", "draft", "approve", "publish", "cleanup"):
        assert callable(getattr(lane, method))
    assert all(
        isinstance(getattr(lane, field), str)
        for field in ("lane_id", "name", "version")
    )


def test_lane_object_lane_id_is_tihna(tmp_path):
    assert _lane(tmp_path).lane_id == "tihna"


def test_ingest_reads_feeds_yaml(tmp_path, monkeypatch):
    seen = []

    def fetch(url, **kwargs):
        seen.append((url, kwargs["category"]))
        return [_entry()]

    monkeypatch.setattr(
        "hermes_cli.lanes.impls.tihna.tihna_rss.fetch_feed",
        fetch,
    )
    _lane(tmp_path).ingest(harness=FakeHarness())
    assert seen == [("https://example.test/feed", "papers")]


def test_ingest_dedups_by_external_id(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lanes.impls.tihna.tihna_rss.fetch_feed",
        lambda *args, **kwargs: [_entry()],
    )
    harness = FakeHarness()
    lane = _lane(tmp_path)
    assert len(lane.ingest(harness=harness)) == 1
    assert lane.ingest(harness=harness) == []
    assert len(harness.tasks) == 1


def test_ingest_creates_lane_task_rows_with_ingested_status(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "hermes_cli.lanes.impls.tihna.tihna_rss.fetch_feed",
        lambda *args, **kwargs: [_entry()],
    )
    task = _lane(tmp_path).ingest(harness=FakeHarness())[0]
    assert task.status == "ingested"
    assert task.payload["feed_url"] == "https://example.test/feed"


def test_ingest_skips_entries_older_than_8_days(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lanes.impls.tihna.tihna_rss.fetch_feed",
        lambda *args, **kwargs: [
            _entry(pub_date="2026-07-01T00:00:00Z")
        ],
    )
    assert _lane(tmp_path).ingest(harness=FakeHarness()) == []


def test_ingest_skips_short_bodies(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lanes.impls.tihna.tihna_rss.fetch_feed",
        lambda *args, **kwargs: [_entry(summary="short")],
    )
    assert _lane(tmp_path).ingest(harness=FakeHarness()) == []


def test_ingest_respects_harness_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lanes.impls.tihna.tihna_rss.fetch_feed",
        lambda *args, **kwargs: [_entry()],
    )
    harness = FakeHarness()
    _lane(tmp_path).ingest(harness=harness)
    assert harness.rate_calls == [
        {"window_kind": "hourly_ingest", "increment": 1}
    ]


def test_ingest_logs_lane_metric_items_ingested(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lanes.impls.tihna.tihna_rss.fetch_feed",
        lambda *args, **kwargs: [_entry()],
    )
    harness = FakeHarness()
    _lane(tmp_path).ingest(harness=harness)
    assert any(name == "items_ingested" for _, name, _ in harness.metrics)


def test_classify_reads_ingested_items_within_7_days(tmp_path):
    harness = FakeHarness()
    _seed_item(harness)
    draft = _lane(tmp_path).draft(
        task=_control("classify"),
        harness=harness,
    )
    assert len(draft.metadata["ranked_items"]) == 1


def test_classify_calls_harness_call_llm_with_purpose_classification(
    tmp_path,
):
    harness = FakeHarness()
    _seed_item(harness)
    _lane(tmp_path).draft(task=_control("classify"), harness=harness)
    assert harness.llm_calls[0]["purpose"] == "classification"
    assert harness.llm_calls[0]["max_tokens"] == 800


def test_classify_persists_scores_back_to_lane_task_payload(tmp_path):
    harness = FakeHarness()
    item = _seed_item(harness)
    _lane(tmp_path).draft(task=_control("classify"), harness=harness)
    updated = harness.find_task(external_id=item.external_id)
    assert updated.payload["score"] == 75
    assert updated.payload["score_reason"] == "strong signal"


def test_classify_gracefully_handles_llm_returning_non_json(tmp_path):
    harness = FakeHarness(llm_text="not-json")
    _seed_item(harness)
    draft = _lane(tmp_path).draft(
        task=_control("classify"),
        harness=harness,
    )
    assert draft.metadata["ranked_items"] == []


def test_draft_selects_only_items_with_score_at_least_60(tmp_path):
    harness = FakeHarness()
    _seed_item(harness, score=59, external_id="low")
    _seed_item(harness, score=60, external_id="kept")
    draft = _lane(tmp_path).draft(
        task=_control("digest"),
        harness=harness,
    )
    assert draft.metadata["selected_item_count"] == 1
    assert '"external_id": "kept"' in harness.llm_calls[0]["prompt"]
    assert '"external_id": "low"' not in harness.llm_calls[0]["prompt"]


def test_draft_calls_harness_call_llm_with_purpose_draft(tmp_path):
    harness = FakeHarness()
    _seed_item(harness, score=70)
    _lane(tmp_path).draft(task=_control("digest"), harness=harness)
    assert harness.llm_calls[0]["purpose"] == "draft"
    assert harness.llm_calls[0]["max_tokens"] == 2500


def test_draft_produces_markdown_with_5_sections(tmp_path):
    harness = FakeHarness()
    _seed_item(harness, score=70)
    draft = _lane(tmp_path).draft(
        task=_control("digest"),
        harness=harness,
    )
    headings = [
        line for line in draft.content.splitlines() if line.startswith("## ")
    ]
    assert len(headings) == 5


def test_draft_runs_output_through_skill_lint_no_op(tmp_path):
    harness = FakeHarness()
    _seed_item(harness, score=70)
    draft = _lane(tmp_path).draft(
        task=_control("digest"),
        harness=harness,
    )
    assert harness.lint_calls == [DIGEST]
    assert draft.content == DIGEST


def test_approve_enqueues_dashboard_approval_with_168h_expiry(tmp_path):
    harness = FakeHarness()
    request = _lane(tmp_path).approve(
        task=_control("digest"),
        draft=LaneDraft(
            DIGEST,
            {"week_label": "2026-W30", "selected_item_count": 2,
             "sections": ["a", "b", "c", "d", "e"]},
        ),
        harness=harness,
    )
    assert request.expires_at == "2026-08-02T12:00:00Z"
    assert harness.approval_drafts[0].metadata["preview"] == DIGEST[:500]


def test_approve_summary_contains_selected_count_and_week_label(tmp_path):
    harness = FakeHarness()
    _lane(tmp_path).approve(
        task=_control("digest"),
        draft=LaneDraft(
            DIGEST,
            {"week_label": "2026-W30", "selected_item_count": 2,
             "sections": ["a", "b", "c", "d", "e"]},
        ),
        harness=harness,
    )
    summary = harness.approval_drafts[0].metadata["summary"]
    assert "2026-W30" in summary
    assert "2 items" in summary


def test_publish_refuses_when_manifest_publish_enabled_false(tmp_path):
    lane = _lane(tmp_path)
    with pytest.raises(PublishDisabled):
        lane.publish(
            task=_control("digest"),
            draft=LaneDraft(DIGEST),
            approval=ApprovalGrant("Token1234567"),
            harness=FakeHarness(publish_enabled=False),
        )
    assert not (tmp_path / "digests").exists()


def test_publish_writes_local_file_when_publish_enabled_true_in_test_manifest(
    tmp_path,
):
    harness = FakeHarness(publish_enabled=True)
    result, external_ref = _lane(tmp_path).publish(
        task=_control("digest"),
        draft=LaneDraft(DIGEST),
        approval=ApprovalGrant("Token1234567"),
        harness=harness,
    )
    output = Path(external_ref)
    assert result.outcome == "success"
    assert output.read_text(encoding="utf-8") == DIGEST
    assert output.stat().st_mode & 0o777 == 0o644
