import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner(config: GatewayConfig) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


@pytest.mark.asyncio
async def test_preprocess_prefixes_sender_for_shared_non_thread_group_session():
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake"),
            },
            group_sessions_per_user=False,
        )
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1002285219667",
        chat_name="Test Group",
        chat_type="group",
        user_name="Alice",
    )
    event = MessageEvent(text="hello", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "[Alice] hello"


@pytest.mark.asyncio
async def test_preprocess_keeps_plain_text_for_default_group_sessions():
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake"),
            },
        )
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1002285219667",
        chat_name="Test Group",
        chat_type="group",
        user_name="Alice",
    )
    event = MessageEvent(text="hello", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.WHATSAPP, Platform.WHATSAPP_CLOUD, Platform.SIGNAL])
async def test_owner_marker_added_for_owner_gated_platforms_in_shared_session(platform):
    """The additive "[SYSTEM: sender NAME is the owner]" marker (Req 3 /
    signalfix.md Gate C) must fire for every platform gated into owner
    detection, not just WhatsApp — Signal opted in alongside WhatsApp/
    WhatsApp Cloud since Signal DMs resolve ``is_owner`` via the generic
    ``_is_owner()`` fallback against ``SIGNAL_ALLOWED_USERS``."""
    runner = _make_runner(
        GatewayConfig(
            platforms={platform: PlatformConfig(enabled=True, token="")},
            group_sessions_per_user=False,
        )
    )
    source = SessionSource(
        platform=platform,
        chat_id="group-1",
        chat_name="Test Group",
        chat_type="group",
        user_name="Alice",
        is_owner=True,
    )
    event = MessageEvent(text="hello", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "[SYSTEM: sender Alice is the owner] [Alice] hello"


@pytest.mark.asyncio
async def test_owner_marker_omitted_for_non_gated_platform_in_shared_session():
    """Platforms that haven't opted into owner detection get the plain sender
    prefix only, even if ``is_owner`` were somehow set."""
    runner = _make_runner(
        GatewayConfig(
            platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
            group_sessions_per_user=False,
        )
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1002285219667",
        chat_name="Test Group",
        chat_type="group",
        user_name="Alice",
        is_owner=True,
    )
    event = MessageEvent(text="hello", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result == "[Alice] hello"
