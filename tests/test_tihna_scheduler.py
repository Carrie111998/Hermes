from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from hermes_cli.cost.kill_switch import (
    KillSwitchTripped,
    PerTaskCapExceeded,
)
from hermes_cli.lanes.contracts import (
    ApprovalRequest,
    LaneTask,
)
from hermes_cli.lanes.impls.tihna import TihnaLane
from hermes_cli.lanes.impls.tihna_scheduler import (
    run_digest,
    run_ingest,
)

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


def _entry() -> dict:
    return {
        "external_id": "entry-1",
        "feed_url": "https://example.test/feed",
        "title": "Synthetic item",
        "link": "https://example.test/entry-1",
        "pub_date": "2026-07-25T10:00:00Z",
        "summary": "x" * 150,
        "author": "Author",
        "tags": [],
        "category": "papers",
    }


def _lane(tmp_path: Path) -> TihnaLane:
    feeds = tmp_path / "feeds.yaml"
    feeds.write_text(
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
    return TihnaLane(
        feeds_path=feeds,
        output_root=tmp_path / "digests",
        now_fn=lambda: NOW,
    )


class SchedulerHarness:
    def __init__(
        self,
        *,
        dry_run: bool = False,
        failure: Exception | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.failure = failure
        self.tasks: list[LaneTask] = []
        self.events: list[str] = []
        self.metrics: list[tuple[str, float]] = []
        self.write_calls = 0

    def find_task(self, *, external_id):
        return next(
            (task for task in self.tasks if task.external_id == external_id),
            None,
        )

    def persist_task(self, task):
        if self.dry_run:
            return task
        self.write_calls += 1
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

    def admit(self, *, task, **_kwargs):
        self.events.append(f"admit:{task.payload.get('stage')}")

    def check_rate_limit(self, **_kwargs):
        if not self.dry_run:
            self.write_calls += 1

    def list_tasks(self, *, status=None, ingested_since=None):
        del ingested_since
        return [
            task
            for task in self.tasks
            if status is None or task.status == status
        ]

    def update_task(self, *, task, payload=None, status=None):
        if not self.dry_run:
            self.write_calls += 1
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

    def call_llm(self, **kwargs):
        purpose = kwargs["purpose"]
        self.events.append(f"llm:{purpose}")
        if self.failure is not None:
            raise self.failure
        text = (
            '[{"external_id":"entry-1","score":75,'
            '"reason":"strong signal"}]'
            if purpose == "classification"
            else DIGEST
        )
        return type("Result", (), {"text": text})()

    def lint_draft(self, text):
        self.events.append("lint")
        return text

    def enqueue_approval(self, *, task, draft):
        del draft
        self.events.append("approve")
        if not self.dry_run:
            self.write_calls += 1
        return ApprovalRequest(
            token="Token1234567",
            lane_task_id=int(task.id or 0),
            status="pending",
            expires_at="2026-08-02T12:00:00Z",
        )

    def record_metric(self, *, task, metric_name, value):
        del task
        self.events.append(f"metric:{metric_name}")
        self.metrics.append((metric_name, float(value)))
        if not self.dry_run:
            self.write_calls += 1


def _seed_item(harness: SchedulerHarness) -> None:
    harness.persist_task(
        LaneTask(
            lane_id="tihna",
            external_id="entry-1",
            task_id="tihna-entry-1",
            payload={**_entry(), "stage": "item"},
        )
    )


def test_scheduler_ingest_stage_returns_new_item_count(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "hermes_cli.lanes.impls.tihna.tihna_rss.fetch_feed",
        lambda *args, **kwargs: [_entry()],
    )
    harness = SchedulerHarness()
    assert run_ingest(lane=_lane(tmp_path), harness=harness) == 1
    assert len(harness.tasks) == 1


def test_scheduler_digest_stage_runs_classify_then_draft_then_approve(
    tmp_path,
):
    harness = SchedulerHarness()
    _seed_item(harness)
    request = run_digest(lane=_lane(tmp_path), harness=harness)
    assert request.status == "pending"
    classify = harness.events.index("llm:classification")
    draft = harness.events.index("llm:draft")
    approve = harness.events.index("approve")
    assert classify < draft < approve


def test_scheduler_digest_stage_halts_on_PerTaskCapExceeded(tmp_path):
    failure = PerTaskCapExceeded(
        task_id="tihna-digest",
        current_total=0.9,
        projected_total=1.1,
        cap=1.0,
    )
    harness = SchedulerHarness(failure=failure)
    _seed_item(harness)
    with pytest.raises(PerTaskCapExceeded):
        run_digest(lane=_lane(tmp_path), harness=harness)
    assert "approve" not in harness.events
    assert any(task.status == "failed" for task in harness.tasks)


def test_scheduler_digest_stage_halts_on_KillSwitchTripped(tmp_path):
    harness = SchedulerHarness(
        failure=KillSwitchTripped(
            task_id="tihna-digest",
            reason="operator",
        )
    )
    _seed_item(harness)
    with pytest.raises(KillSwitchTripped):
        run_digest(lane=_lane(tmp_path), harness=harness)
    assert "approve" not in harness.events
    assert any(task.status == "failed" for task in harness.tasks)


def test_scheduler_digest_stage_records_metrics_at_cleanup(tmp_path):
    harness = SchedulerHarness()
    _seed_item(harness)
    run_digest(lane=_lane(tmp_path), harness=harness)
    assert {name for name, _ in harness.metrics} == {
        "digest_selected_count",
        "digest_total_items",
        "digest_avg_score",
        "ingested_this_window",
    }


def test_scheduler_dry_run_flag_produces_no_writes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "hermes_cli.lanes.impls.tihna.tihna_rss.fetch_feed",
        lambda *args, **kwargs: [_entry()],
    )
    harness = SchedulerHarness(dry_run=True)
    assert run_ingest(lane=_lane(tmp_path), harness=harness) == 1
    assert harness.write_calls == 0
    assert harness.tasks == []
