"""Tests for event-bus clean delivery path (no cron framing)."""
from unittest.mock import patch, MagicMock


def _make_gateway_config():
    """Build a minimal gateway config object with telegram enabled."""
    from gateway.config import Platform
    pconfig = MagicMock()
    pconfig.enabled = True
    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: pconfig}
    return config


def test_deliver_result_with_skip_cron_framing_omits_wrapper():
    """Event-bus subscribers get clean output, not 'Cronjob Response' wrapped."""
    from cron.scheduler import _deliver_result

    captured = {}

    async def fake_send_to_platform(platform, pconfig, chat_id, content, thread_id=None, media_files=None, buttons=None):
        captured["text"] = content
        captured["thread_id"] = thread_id
        return {"ok": True}

    with patch("tools.send_message_tool._send_to_platform", side_effect=fake_send_to_platform), \
         patch("gateway.config.load_gateway_config", return_value=_make_gateway_config()):
        _deliver_result(
            {"deliver": "telegram:-100:15", "id": "event-bus", "name": "event-bus"},
            "my payload line\nother line",
            skip_cron_framing=True,
        )

    assert "Cronjob Response" not in captured.get("text", "")
    assert "To stop or manage" not in captured.get("text", "")
    assert "my payload line" in captured["text"]


def test_deliver_result_without_skip_keeps_wrapper_for_backcompat():
    """Cron-originated deliveries still get the wrapper (existing behavior)."""
    from cron.scheduler import _deliver_result

    captured = {}

    async def fake_send_to_platform(platform, pconfig, chat_id, content, thread_id=None, media_files=None, buttons=None):
        captured["text"] = content
        return {"ok": True}

    with patch("tools.send_message_tool._send_to_platform", side_effect=fake_send_to_platform), \
         patch("gateway.config.load_gateway_config", return_value=_make_gateway_config()):
        _deliver_result(
            {"deliver": "telegram:-100:15", "id": "my-cron-job", "name": "my-cron-job"},
            "cron output text",
        )

    assert "Cronjob Response" in captured.get("text", "")
