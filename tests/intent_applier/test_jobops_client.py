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

    def test_read_timeout_raises_transient(self):
        """A socket READ-timeout must map to transient, not escape uncaught.

        Regression for 2026-07-12: ``urlopen(req, timeout=N)`` raises a *bare*
        ``TimeoutError`` from ``getresponse()`` (the response-read phase) when
        the server accepts the connection but hangs — e.g. :4100 under a
        Temporal-down 500-storm. ``TimeoutError`` (== ``socket.timeout`` on
        3.10+) is an ``OSError`` subclass but NOT a ``URLError``, so the old
        handler chain (HTTPError / URLError / JSONDecodeError) missed it. The
        uncaught error propagated up through ``apply_one`` -> ``scan_inbox`` ->
        the subscriber ``poll()``, crashing the gateway poll tick, leaving the
        intent in the inbox for reprocessing, and emitting a duplicate
        PIPELINE_UPDATE mirror on the retry.
        """
        client = JobOpsClient(base_url="http://stub:4100", timeout_seconds=5)
        with patch("intent_applier.jobops_client.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = TimeoutError("timed out")
            with pytest.raises(JobOpsClientTransientError, match="timed out"):
                client.post_legacy_stage(
                    job_id="job-1", stage="approved",
                    actor_id="tracker", source="tracker_mailbox", notes="",
                )

    def test_socket_oserror_raises_transient(self):
        """Other socket-level OSErrors (conn reset, broken pipe) are transient too."""
        client = JobOpsClient(base_url="http://stub:4100", timeout_seconds=5)
        with patch("intent_applier.jobops_client.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = ConnectionResetError("connection reset by peer")
            with pytest.raises(JobOpsClientTransientError, match="reset by peer"):
                client.post_legacy_stage(
                    job_id="job-1", stage="approved",
                    actor_id="tracker", source="tracker_mailbox", notes="",
                )
