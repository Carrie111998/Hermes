import json
from unittest.mock import MagicMock

import pytest

from pipeline_state import PipelineManager
from reply_handlers.parser import CommandIntent
from reply_handlers.executor import (
    execute,
    CommandResult,
    VERB_TO_STAGE,
    VERB_TO_APPROVAL,
)


@pytest.fixture
def pipeline_path(tmp_path):
    """Temp pipeline.json with one seeded job at stage=review."""
    p = tmp_path / "pipeline.json"
    p.write_text(json.dumps({
        "jobs": [{
            "job_id": "linkedin-1",
            "stage": "review",
            "title": "VP AI",
            "company": "Acme",
            "history": [],
        }],
        "stats": {},
    }))
    return p


@pytest.fixture
def mgr(pipeline_path):
    return PipelineManager(path=pipeline_path)


class TestExecutorHappyPath:
    def test_approve_writes_stage_and_returns_ok(self, mgr, pipeline_path):
        intent = CommandIntent(verb="approve", job_id="linkedin-1", reason="great fit")
        result = execute(intent, actor="diego", source="telegram", manager=mgr)
        assert result.ok is True
        assert result.new_stage == "approved"
        data = json.loads(pipeline_path.read_text())
        job = next(j for j in data["jobs"] if j["job_id"] == "linkedin-1")
        assert job["stage"] == "approved"
        assert job["history"][-1]["source"] == "telegram"
        assert job["history"][-1]["agent"] == "diego"
        assert "great fit" in job["history"][-1]["notes"]

    def test_reject_writes_rejected_by_user(self, mgr):
        intent = CommandIntent(verb="reject", job_id="linkedin-1", reason="too junior")
        result = execute(intent, actor="diego", source="whatsapp", manager=mgr)
        assert result.ok is True
        assert result.new_stage == "rejected_by_user"

    def test_archive_writes_archived(self, mgr):
        intent = CommandIntent(verb="archive", job_id="linkedin-1")
        result = execute(intent, actor="diego", source="telegram", manager=mgr)
        assert result.ok is True
        assert result.new_stage == "archived"

    def test_message_includes_job_id_and_stage(self, mgr):
        intent = CommandIntent(verb="approve", job_id="linkedin-1")
        result = execute(intent, actor="diego", source="telegram", manager=mgr)
        assert "linkedin-1" in result.message
        assert "approved" in result.message


class TestExecutorMissingJob:
    def test_unknown_job_returns_not_ok(self, mgr):
        intent = CommandIntent(verb="approve", job_id="does-not-exist")
        result = execute(intent, actor="diego", source="telegram", manager=mgr)
        assert result.ok is False
        assert "not found" in result.message.lower()


class TestExecutorResume:
    def test_resume_called_with_correct_payload_on_approve(self, mgr):
        resume = MagicMock()
        intent = CommandIntent(verb="approve", job_id="linkedin-1")
        execute(intent, actor="diego", source="telegram", manager=mgr, resume_full=resume)
        resume.assert_called_once()
        args, _ = resume.call_args
        assert args[0] == "job-linkedin-1"
        assert args[1] == {"approval": "yes"}

    def test_resume_called_with_no_on_reject(self, mgr):
        resume = MagicMock()
        intent = CommandIntent(verb="reject", job_id="linkedin-1")
        execute(intent, actor="diego", source="telegram", manager=mgr, resume_full=resume)
        args, _ = resume.call_args
        assert args[1] == {"approval": "no"}

    def test_resume_called_with_no_on_archive(self, mgr):
        resume = MagicMock()
        intent = CommandIntent(verb="archive", job_id="linkedin-1")
        execute(intent, actor="diego", source="telegram", manager=mgr, resume_full=resume)
        args, _ = resume.call_args
        assert args[1] == {"approval": "no"}

    def test_resume_failure_does_not_break_executor(self, mgr):
        """resume_full may raise if no thread is paused; non-fatal."""
        resume = MagicMock(side_effect=KeyError("thread not found"))
        intent = CommandIntent(verb="approve", job_id="linkedin-1")
        result = execute(intent, actor="diego", source="telegram", manager=mgr, resume_full=resume)
        assert result.ok is True
        assert result.new_stage == "approved"

    def test_no_resume_callable_skips_resume(self, mgr):
        intent = CommandIntent(verb="approve", job_id="linkedin-1")
        result = execute(intent, actor="diego", source="telegram", manager=mgr, resume_full=None)
        assert result.ok is True


class TestVerbMaps:
    def test_all_valid_verbs_have_stage_mapping(self):
        from reply_handlers.parser import VALID_VERBS
        for v in VALID_VERBS:
            assert v in VERB_TO_STAGE, f"missing stage mapping for /{v}"
            assert v in VERB_TO_APPROVAL, f"missing approval mapping for /{v}"

    def test_all_target_stages_are_in_pipeline_state_valid_stages(self):
        from pipeline_state.manager import VALID_STAGES
        for verb, stage in VERB_TO_STAGE.items():
            assert stage in VALID_STAGES, f"verb /{verb} maps to unknown stage {stage!r}"
