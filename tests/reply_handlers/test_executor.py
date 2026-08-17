from unittest.mock import MagicMock

import pytest

from reply_handlers.parser import CommandIntent
from reply_handlers.executor import (
    execute,
    VERB_TO_STAGE,
    VERB_TO_APPROVAL,
)


@pytest.fixture
def mock_jobops():
    client = MagicMock()
    client.post_intent.return_value = {
        "queued": True,
        "messageId": "msg-1",
        "mode": "tracker_intent",
    }
    return client


class TestExecutorHappyPath:
    def test_approve_posts_intent(self, mock_jobops):
        intent = CommandIntent(verb="approve", job_id="linkedin-1", reason="great fit")
        result = execute(intent, actor="diego", source="telegram", jobops_client=mock_jobops)
        assert result.ok is True
        assert result.new_stage == "approved"
        mock_jobops.post_intent.assert_called_once()
        call = mock_jobops.post_intent.call_args
        assert call.kwargs["job_id"] == "linkedin-1"
        assert call.kwargs["stage"] == "approved"
        assert call.kwargs["actor_id"] == "diego"
        assert call.kwargs["source"] == "telegram"

    def test_reject_posts_intent_with_rejected(self, mock_jobops):
        """Reject maps to 'rejected' (in JobOps LEGACY_PIPELINE_STAGES), not
        'rejected_by_user' which is PipelineManager-internal and would be 400'd
        by JobOps's stage validator."""
        intent = CommandIntent(verb="reject", job_id="j-1", reason="too junior")
        result = execute(intent, actor="diego", source="whatsapp", jobops_client=mock_jobops)
        assert result.ok is True
        assert result.new_stage == "rejected"
        assert mock_jobops.post_intent.call_args.kwargs["stage"] == "rejected"

    def test_archive_posts_intent(self, mock_jobops):
        intent = CommandIntent(verb="archive", job_id="j-1")
        result = execute(intent, actor="diego", source="telegram", jobops_client=mock_jobops)
        assert result.ok is True
        assert result.new_stage == "archived"


class TestExecutorMetadata:
    def test_passes_original_source_in_metadata(self, mock_jobops):
        intent = CommandIntent(verb="approve", job_id="j-1")
        execute(intent, actor="diego", source="telegram", jobops_client=mock_jobops)
        meta = mock_jobops.post_intent.call_args.kwargs["metadata"]
        assert meta["original_source"] == "telegram"

    def test_thread_id_passed_when_provided(self, mock_jobops):
        intent = CommandIntent(verb="approve", job_id="j-1")
        execute(
            intent, actor="diego", source="telegram",
            jobops_client=mock_jobops, thread_id="job-j-1",
        )
        meta = mock_jobops.post_intent.call_args.kwargs["metadata"]
        assert meta["thread_id"] == "job-j-1"

    def test_thread_id_omitted_when_not_provided(self, mock_jobops):
        """Bare execute() call with no thread_id leaves it absent from metadata."""
        intent = CommandIntent(verb="approve", job_id="j-1")
        execute(intent, actor="diego", source="telegram", jobops_client=mock_jobops)
        meta = mock_jobops.post_intent.call_args.kwargs["metadata"]
        assert "thread_id" not in meta


class TestExecutorErrors:
    def test_jobops_404_returns_not_ok(self, mock_jobops):
        from intent_applier import JobOpsClientPermanentError
        mock_jobops.post_intent.side_effect = JobOpsClientPermanentError(
            "JobOps API ... returned 404 Not Found"
        )
        intent = CommandIntent(verb="approve", job_id="missing")
        result = execute(intent, actor="diego", source="telegram", jobops_client=mock_jobops)
        assert result.ok is False
        assert "not found" in result.message.lower() or "404" in result.message

    def test_jobops_unreachable_returns_not_ok(self, mock_jobops):
        from intent_applier import JobOpsClientTransientError
        mock_jobops.post_intent.side_effect = JobOpsClientTransientError(
            "JobOps API ... unreachable: Connection refused"
        )
        intent = CommandIntent(verb="approve", job_id="j-1")
        result = execute(intent, actor="diego", source="telegram", jobops_client=mock_jobops)
        assert result.ok is False
        assert "unreachable" in result.message.lower() or "could not" in result.message.lower()


class TestVerbMaps:
    def test_all_verbs_have_stage_mapping(self):
        from reply_handlers.parser import VALID_VERBS
        for v in VALID_VERBS:
            assert v in VERB_TO_STAGE
            assert v in VERB_TO_APPROVAL
