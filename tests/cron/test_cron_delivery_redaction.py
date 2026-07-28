"""Cron delivery text must be secret-redacted before it reaches any platform.

Shell-job stdout/stderr is redacted where it is captured, but an LLM cron job's
response text reached ``_deliver_result`` unscanned, so a job that surfaced a
credential in its answer delivered it verbatim to the chat.
"""
from unittest.mock import AsyncMock, MagicMock, patch


def _telegram_cfg():
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    mock_cfg = MagicMock()
    mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
    return mock_cfg


def _job():
    return {
        "id": "report-job",
        "name": "daily-report",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
    }


# A synthetic OpenAI-style key: long enough to trip the redactor, and not a
# real credential.
FAKE_SECRET = "sk-" + "A" * 32


class TestCronDeliveryRedaction:
    def test_standalone_delivery_redacts_secret(self):
        """A secret in the job's answer must not reach the platform send.

        Redaction happens once on ``cleaned_delivery_content`` right after
        media extraction — i.e. before the live-adapter / standalone branch —
        so both delivery paths consume the same redacted string. This test
        drives the standalone branch; the live-adapter branch reads the very
        same variable.
        """
        from cron.scheduler import _deliver_result

        send_mock = AsyncMock(return_value={"success": True})
        with patch("gateway.config.load_gateway_config", return_value=_telegram_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=send_mock), \
             patch("sys.is_finalizing", return_value=False):
            _deliver_result(_job(), f"Job finished. Token was {FAKE_SECRET} (oops).")

        send_mock.assert_called_once()
        delivered = " ".join(str(a) for a in send_mock.call_args.args)
        delivered += " " + " ".join(str(v) for v in send_mock.call_args.kwargs.values())
        assert FAKE_SECRET not in delivered, "secret reached the platform send"

    def test_clean_content_is_unchanged(self):
        """Redaction must not mangle ordinary delivery text."""
        from cron.scheduler import _deliver_result

        body = "Daily report: 3 tasks done, 1 pending. All systems nominal."
        send_mock = AsyncMock(return_value={"success": True})
        with patch("gateway.config.load_gateway_config", return_value=_telegram_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=send_mock), \
             patch("sys.is_finalizing", return_value=False):
            result = _deliver_result(_job(), body)

        send_mock.assert_called_once()
        delivered = " ".join(str(a) for a in send_mock.call_args.args)
        assert "3 tasks done" in delivered
        assert result is None

    def test_redaction_failure_does_not_leak(self):
        """If the redactor itself raises, the delivery must fail closed rather
        than send the unscanned text."""
        from cron.scheduler import _deliver_result

        send_mock = AsyncMock(return_value={"success": True})
        with patch("gateway.config.load_gateway_config", return_value=_telegram_cfg()), \
             patch("tools.send_message_tool._send_to_platform", new=send_mock), \
             patch("agent.redact.redact_sensitive_text", side_effect=RuntimeError("boom")), \
             patch("sys.is_finalizing", return_value=False):
            _deliver_result(_job(), f"Token {FAKE_SECRET}")

        send_mock.assert_called_once()
        delivered = " ".join(str(a) for a in send_mock.call_args.args)
        assert FAKE_SECRET not in delivered
        assert "REDACTED" in delivered
