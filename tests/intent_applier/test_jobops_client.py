from unittest.mock import MagicMock, patch

import pytest

from intent_applier.jobops_client import (
    JobOpsClient,
    JobOpsClientError,
    JobOpsClientPermanentError,
    JobOpsClientTransientError,
)


class TestJobOpsClient:
    def test_post_intent_success(self):
        client = JobOpsClient(base_url="http://stub:4100", timeout_seconds=5)
        with patch("intent_applier.jobops_client.urlopen") as mock_urlopen:
            response = MagicMock()
            response.status = 202
            response.read.return_value = b'{"queued":true,"messageId":"m-1"}'
            mock_urlopen.return_value.__enter__.return_value = response
            result = client.post_intent(
                job_id="job-1",
                stage="approved",
                actor_id="diego",
                source="legacy_dashboard",
                notes="ok",
                metadata={"foo": "bar"},
            )
        assert result["queued"] is True
        assert result["messageId"] == "m-1"
        # Verify URL + body shape
        args, _kwargs = mock_urlopen.call_args
        request = args[0]
        assert "/api/v1/jobs/job-1/intents/transition" in request.full_url

    def test_post_legacy_stage_success(self):
        """Used by the applier to write Postgres via tracker_only-allowed source."""
        client = JobOpsClient(base_url="http://stub:4100", timeout_seconds=5)
        with patch("intent_applier.jobops_client.urlopen") as mock_urlopen:
            response = MagicMock()
            response.status = 200
            response.read.return_value = b'{"success":true,"job":{"id":"job-1"}}'
            mock_urlopen.return_value.__enter__.return_value = response
            result = client.post_legacy_stage(
                job_id="job-1",
                stage="approved",
                actor_id="tracker",
                source="tracker_mailbox",
                notes="applied",
            )
        assert result["success"] is True

    def test_5xx_raises_transient(self):
        from urllib.error import HTTPError
        client = JobOpsClient(base_url="http://stub:4100", timeout_seconds=5)
        with patch("intent_applier.jobops_client.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = HTTPError(
                url="http://stub", code=502, msg="Bad Gateway", hdrs=None, fp=None,
            )
            with pytest.raises(JobOpsClientTransientError, match="502"):
                client.post_legacy_stage(
                    job_id="job-1", stage="approved",
                    actor_id="tracker", source="tracker_mailbox", notes="",
                )

    def test_4xx_raises_permanent(self):
        from urllib.error import HTTPError
        from io import BytesIO
        client = JobOpsClient(base_url="http://stub:4100", timeout_seconds=5)
        with patch("intent_applier.jobops_client.urlopen") as mock_urlopen:
            err = HTTPError(
                url="http://stub", code=400, msg="Bad Request", hdrs=None,
                fp=BytesIO(b'{"error":"invalid stage"}'),
            )
            mock_urlopen.side_effect = err
            with pytest.raises(JobOpsClientPermanentError, match="400"):
                client.post_legacy_stage(
                    job_id="job-1", stage="approved",
                    actor_id="tracker", source="tracker_mailbox", notes="",
                )

    def test_connection_refused_raises_transient(self):
        from urllib.error import URLError
        client = JobOpsClient(base_url="http://stub:4100", timeout_seconds=5)
        with patch("intent_applier.jobops_client.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = URLError("Connection refused")
            with pytest.raises(JobOpsClientTransientError, match="Connection refused"):
                client.post_legacy_stage(
                    job_id="job-1", stage="approved",
                    actor_id="tracker", source="tracker_mailbox", notes="",
                )
