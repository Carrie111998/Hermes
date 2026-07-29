"""Per-platform streaming defaults + dashboard exposure.

Streaming is smooth on Telegram (native sendMessageDraft) but flickers on
edit-only platforms like Discord and Slack (repeated editMessage). The shipped
defaults encode that: display.platforms.telegram.streaming=true,
.discord.streaming=false, .slack.streaming=false. These are gap-fillers (user
values win via deep-merge) and, because the dashboard schema is generated from
DEFAULT_CONFIG, they automatically appear as editable toggles in the web UI.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("enabled", "transport", "platform", "expected"),
    [
        (False, "auto", "telegram", False),
        (False, "auto", "slack", False),
        (True, "auto", "telegram", True),
        (True, "auto", "discord", False),
        (True, "auto", "slack", True),
        (True, "off", "telegram", False),
        (True, "off", "slack", False),
    ],
)
def test_global_streaming_is_master_gate(enabled, transport, platform, expected):
    from gateway.config import StreamingConfig
    from gateway.display_config import resolve_streaming_enabled
    from hermes_cli.config import DEFAULT_CONFIG

    config = dict(DEFAULT_CONFIG)
    config["streaming"] = {"enabled": enabled, "transport": transport}
    streaming = StreamingConfig.from_dict(config["streaming"])

    assert resolve_streaming_enabled(config, platform, streaming) is expected


def test_default_per_platform_streaming_flags():
    from hermes_cli.config import DEFAULT_CONFIG
    plats = DEFAULT_CONFIG["display"]["platforms"]
    assert plats["telegram"]["streaming"] is True
    assert plats["discord"]["streaming"] is False
    assert plats["slack"]["streaming"] is False


