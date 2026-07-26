from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_cli.cost.kill_switch import (
    KillSwitchTripped,
    PerTaskCapExceeded,
)
from hermes_cli.lanes.contracts import ApprovalGrant, LaneDraft, LaneTask
from hermes_cli.lanes.dry_run import (
    FakeLLMCaller,
    run_lane_dry_run,
)
from hermes_cli.lanes.errors import PublishDisabled
from hermes_cli.lanes.harness import DryRunHarness, DryRunViolation


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "lane_manifest.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "lanes": [
                    {
                        "lane_id": "tihna",
                        "enabled": False,
                        "module": "hermes_cli.lanes.impls.tihna",
                        "approval_channel": "dashboard",
                        "approval_timeout_hours": 168,
                        "per_lane_daily_cost_cap_aud": 2.0,
                        "per_lane_daily_task_cap": 15,
                        "per_lane_hourly_ingest_cap": 5,
                        "publish_enabled": False,
                        "description": "Tihna test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "kanban.db"
    path.write_bytes(b"dry-run-sentinel")
    return path


def _run(tmp_path: Path, stage="full", **kwargs):
    return run_lane_dry_run(
        "tihna",
        stage=stage,
        manifest_path=_manifest(tmp_path),
        db_path=_db(tmp_path),
        **kwargs,
    )


def _harness(tmp_path: Path, **kwargs) -> DryRunHarness:
    return DryRunHarness(
        lane_id="tihna",
        db_path=_db(tmp_path),
        manifest_path=_manifest(tmp_path),
        llm_caller=FakeLLMCaller(),
        **kwargs,
    )


def _task() -> LaneTask:
    return LaneTask(
        lane_id="tihna",
        external_id="dry-run-task",
        task_id="dry-run-task",
        id=1,
        payload={"stage": "digest"},
    )


def test_dry_run_ingest_stage_completes_with_zero_kanban_writes(
    tmp_path,
):
    report = _run(tmp_path, stage="ingest")
    assert report.success is True
    assert report.ingested > 0
    assert report.kanban_writes == 0


def test_dry_run_digest_stage_completes_with_zero_kanban_writes(
    tmp_path,
):
    report = _run(tmp_path, stage="digest")
    assert report.success is True
    assert report.classified > 0
    assert report.drafted == 1
    assert report.kanban_writes == 0


def test_dry_run_full_stage_completes_with_zero_kanban_writes(tmp_path):
    db = _db(tmp_path)
    before = db.read_bytes()
    report = run_lane_dry_run(
        "tihna",
        stage="full",
        manifest_path=_manifest(tmp_path),
        db_path=db,
    )
    assert report.success is True
    assert db.read_bytes() == before


def test_dry_run_uses_fixture_feed_not_network(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: pytest.fail("network was called"),
    )
    report = _run(tmp_path)
    assert report.success is True
    assert report.fixture_feed_used is True


def test_dry_run_uses_fake_llm_caller_not_real_provider(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "hermes_cli.lanes.impls.tihna_scheduler.default_llm_caller",
        lambda **kwargs: pytest.fail("real provider caller was reached"),
    )
    report = _run(tmp_path)
    assert report.success is True
    assert report.fake_llm_used is True


def test_dry_run_reports_ingested_classified_drafted_counts(tmp_path):
    report = _run(tmp_path)
    assert report.ingested == 2
    assert report.classified == 2
    assert report.drafted == 1
    assert report.approvals_enqueued == 1


def test_dry_run_never_calls_real_publish_when_publish_enabled_false(
    tmp_path,
):
    report = _run(tmp_path)
    assert report.publish_would_have_been_called is False
    assert not (tmp_path / "tihna-digests").exists()


def test_dry_run_respects_per_task_cost_cap_from_CS10a(tmp_path):
    harness = _harness(tmp_path, task_cap_aud=0.005)
    with pytest.raises(PerTaskCapExceeded):
        harness.call_llm(
            task=_task(),
            prompt="fixture",
            max_tokens=10,
            purpose="draft",
        )


def test_dry_run_respects_kill_switch_from_CS10a(tmp_path):
    harness = _harness(tmp_path)
    harness.kill_task("dry-run-task", reason="operator")
    with pytest.raises(KillSwitchTripped):
        harness.call_llm(
            task=_task(),
            prompt="fixture",
            max_tokens=10,
            purpose="draft",
        )


def test_dry_run_records_simulated_llm_cost(tmp_path):
    report = _run(tmp_path)
    assert report.simulated_llm_cost_aud == pytest.approx(0.02)
    assert report.cost_ledger_writes == 0


def test_dry_run_raises_DryRunViolation_on_attempted_real_write(
    tmp_path,
):
    harness = _harness(tmp_path)
    with pytest.raises(DryRunViolation):
        harness.attempt_real_write("kanban.db")


def test_dry_run_harness_publish_with_ledger_refuses_when_manifest_publish_false(
    tmp_path,
):
    harness = _harness(tmp_path)
    with pytest.raises(PublishDisabled):
        harness.publish_with_ledger(
            task=_task(),
            external_target="local:file:test",
            payload={"approval": ApprovalGrant("DRYRUN000001").token},
            publisher=lambda _payload: pytest.fail(
                "publisher callback was reached"
            ),
        )


def test_dry_run_does_not_write_side_effects_ledger(tmp_path):
    report = _run(tmp_path)
    assert report.side_effect_writes == 0
    assert report.publish_would_have_been_called is False


def test_dry_run_does_not_write_cost_ledger(tmp_path):
    report = _run(tmp_path)
    assert report.cost_ledger_writes == 0
    assert report.simulated_llm_cost_aud > 0


def test_dry_run_does_not_write_lane_task_or_approval_or_publish_log(
    tmp_path,
):
    db = _db(tmp_path)
    before = db.read_bytes()
    report = run_lane_dry_run(
        "tihna",
        manifest_path=_manifest(tmp_path),
        db_path=db,
    )
    assert report.kanban_writes == 0
    assert db.read_bytes() == before


def test_dry_run_returns_exit_1_on_any_stage_failure(tmp_path):
    def fail(**_kwargs):
        raise RuntimeError("synthetic stage failure")

    report = _run(tmp_path, llm_caller=fail)
    assert report.success is False
    assert report.exit_code == 1
    assert "synthetic stage failure" in report.error
