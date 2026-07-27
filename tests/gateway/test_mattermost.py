"""Tests for Mattermost platform adapter."""
import json
import os
import subprocess
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
from gateway.run import (
    _resolve_gateway_display_bool,
    _resolve_progress_thread_id,
)


class TestMattermostProgressThreadRouting:
    def test_top_level_mattermost_progress_uses_event_message_id(self):
        assert _resolve_progress_thread_id(
            Platform.MATTERMOST,
            source_thread_id=None,
            event_message_id="top_post_123",
        ) == "top_post_123"

    def test_threaded_mattermost_progress_prefers_existing_thread_root(self):
        assert _resolve_progress_thread_id(
            Platform.MATTERMOST,
            source_thread_id="root_post_123",
            event_message_id="reply_post_456",
        ) == "root_post_123"

    def test_telegram_progress_does_not_use_message_id_as_thread_id(self):
        assert _resolve_progress_thread_id(
            Platform.TELEGRAM,
            source_thread_id=None,
            event_message_id="12345",
        ) is None


class TestMattermostDisplayHygiene:
    def test_mattermost_requires_platform_opt_in_for_interim_assistant_messages(self):
        """Global interim commentary must not make Mattermost leak scratch notes."""
        user_config = {"display": {"interim_assistant_messages": True}}

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "interim_assistant_messages",
            default=True,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is False

    def test_mattermost_platform_opt_in_can_enable_interim_assistant_messages(self):
        """Mattermost can still opt into commentary explicitly per platform."""
        user_config = {
            "display": {
                "interim_assistant_messages": False,
                "platforms": {
                    "mattermost": {"interim_assistant_messages": True},
                },
            }
        }

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "interim_assistant_messages",
            default=True,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is True

    def test_mattermost_requires_platform_opt_in_for_thinking_progress(self):
        """Global thinking_progress must not surface internal analysis in Mattermost."""
        user_config = {"display": {"thinking_progress": True}}

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "thinking_progress",
            default=False,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is False

    def test_mattermost_requires_platform_opt_in_for_show_reasoning(self):
        """Global show_reasoning must not prepend scratch reasoning in Mattermost."""
        user_config = {"display": {"show_reasoning": True}}

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "show_reasoning",
            default=False,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is False

    def test_mattermost_platform_opt_in_can_enable_show_reasoning(self):
        user_config = {
            "display": {
                "show_reasoning": False,
                "platforms": {"mattermost": {"show_reasoning": True}},
            }
        }

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "show_reasoning",
            default=False,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is True

    def test_global_thinking_progress_still_applies_to_other_platforms(self):
        """The Mattermost guard must not silently neuter Telegram/other chats."""
        user_config = {"display": {"thinking_progress": True}}

        assert _resolve_gateway_display_bool(
            user_config,
            "telegram",
            "thinking_progress",
            default=False,
            platform=Platform.TELEGRAM,
            require_platform_override_for={Platform.MATTERMOST},
        ) is True


# ---------------------------------------------------------------------------
# Platform & Config
# ---------------------------------------------------------------------------

class TestMattermostConfigLoading:
    def test_apply_env_overrides_mattermost(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "mm-tok-abc123")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.MATTERMOST in config.platforms
        mc = config.platforms[Platform.MATTERMOST]
        assert mc.enabled is True
        assert mc.token == "mm-tok-abc123"
        assert mc.extra.get("url") == "https://mm.example.com"

    def test_mattermost_not_loaded_without_token(self, monkeypatch):
        monkeypatch.delenv("MATTERMOST_TOKEN", raising=False)
        monkeypatch.delenv("MATTERMOST_URL", raising=False)

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.MATTERMOST not in config.platforms

    def test_mattermost_home_channel(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "mm-tok-abc123")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")
        monkeypatch.setenv("MATTERMOST_HOME_CHANNEL", "ch_abc123")
        monkeypatch.setenv("MATTERMOST_HOME_CHANNEL_NAME", "General")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        home = config.get_home_channel(Platform.MATTERMOST)
        assert home is not None
        assert home.chat_id == "ch_abc123"
        assert home.name == "General"

    def test_mattermost_url_warning_without_url(self, monkeypatch):
        """MATTERMOST_TOKEN set but MATTERMOST_URL missing should still load."""
        monkeypatch.setenv("MATTERMOST_TOKEN", "mm-tok-abc123")
        monkeypatch.delenv("MATTERMOST_URL", raising=False)

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.MATTERMOST in config.platforms
        assert config.platforms[Platform.MATTERMOST].extra.get("url") == ""


# ---------------------------------------------------------------------------
# Adapter format / truncate
# ---------------------------------------------------------------------------

def _make_adapter():
    """Create a MattermostAdapter with mocked config."""
    from plugins.platforms.mattermost.adapter import MattermostAdapter
    config = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={"url": "https://mm.example.com"},
    )
    adapter = MattermostAdapter(config)
    return adapter


class TestMattermostFormatMessage:
    def setup_method(self):
        self.adapter = _make_adapter()

    def test_image_markdown_to_url(self):
        """![alt](url) should be converted to just the URL."""
        result = self.adapter.format_message("![cat](https://img.example.com/cat.png)")
        assert result == "https://img.example.com/cat.png"

    def test_image_markdown_strips_alt_text(self):
        result = self.adapter.format_message("Here: ![my image](https://x.com/a.jpg) done")
        assert "![" not in result
        assert "https://x.com/a.jpg" in result

    def test_regular_markdown_preserved(self):
        """Regular markdown (bold, italic, code) should be kept as-is."""
        content = "**bold** and *italic* and `code`"
        assert self.adapter.format_message(content) == content

    def test_regular_links_preserved(self):
        """Non-image links should be preserved."""
        content = "[click](https://example.com)"
        assert self.adapter.format_message(content) == content

    def test_plain_text_unchanged(self):
        content = "Hello, world!"
        assert self.adapter.format_message(content) == content

    def test_multiple_images(self):
        content = "![a](http://a.com/1.png) text ![b](http://b.com/2.png)"
        result = self.adapter.format_message(content)
        assert "![" not in result
        assert "http://a.com/1.png" in result
        assert "http://b.com/2.png" in result


class TestMattermostHiggsfieldBypassDetection:
    def test_detects_nano_banana_pro_job_type(self):
        from plugins.platforms.mattermost.adapter import _detect_higgsfield_image_job_type

        assert _detect_higgsfield_image_job_type("모델: Nano Banana 2 Pro") == "nano_banana_pro"

    def test_detects_seedream_job_type(self):
        from plugins.platforms.mattermost.adapter import _detect_higgsfield_image_job_type

        assert _detect_higgsfield_image_job_type("Seedream 4.5로 얼굴 유지") == "seedream_v4_5"
        assert _detect_higgsfield_image_job_type("Seedream 5.0 Pro로 고품질 이미지") == "seedream_v5_pro"

    def test_image_edit_requires_image_attachment(self):
        from plugins.platforms.mattermost.adapter import _looks_like_higgsfield_image_edit_request

        text = "Nano Banana 2 Pro 요청: 옷만 정장으로 변경"
        assert _looks_like_higgsfield_image_edit_request(text, has_image=True) is True
        assert _looks_like_higgsfield_image_edit_request(text, has_image=False) is False

    def test_extracts_aspect_and_resolution(self):
        from plugins.platforms.mattermost.adapter import _extract_higgsfield_generation_params

        assert _extract_higgsfield_generation_params("비율: 1:1 해상도: 1K") == ("1:1", "1k")

    def test_confirmation_and_cancel_words(self):
        from plugins.platforms.mattermost.adapter import _is_higgsfield_cancel, _is_higgsfield_confirmation

        assert _is_higgsfield_confirmation("진행해줘") is True
        assert _is_higgsfield_confirmation("네") is True
        assert _is_higgsfield_cancel("취소") is True

    @pytest.mark.asyncio
    async def test_image_edit_request_launches_without_confirmation_prompt(self):
        adapter = _make_adapter()

        async def fake_cli(args, *, timeout=600.0):
            assert args[:3] == ["generate", "cost", "nano_banana_pro"]
            assert "--image-references" in args
            assert "--image" not in args
            assert "C:/tmp/input.png" in args
            prompt = args[args.index("--prompt") + 1]
            assert "Use the attached image as the primary reference" in prompt
            assert "User request: Nano Banana 2 Pro 요청" in prompt
            return 0, "2 credits\n", ""

        adapter._run_higgsfield_cli = fake_cli
        adapter.send = AsyncMock()

        with patch("plugins.platforms.mattermost.adapter.asyncio.create_task") as create_task:
            handled = await adapter._maybe_start_higgsfield_image_edit(
                channel_id="channel_1",
                sender_id="user_1",
                post_id="post_1",
                message_text="Nano Banana 2 Pro 요청: 옷만 정장으로 변경",
                media_urls=["C:/tmp/input.png"],
                media_types=["image/png"],
            )

        assert handled is True
        adapter.send.assert_not_called()
        assert adapter._pending_higgsfield_edits == {}
        create_task.assert_called_once()
        create_task.call_args.args[0].close()

    @pytest.mark.asyncio
    async def test_image_edit_channel_forces_higgsfield_for_attached_images(self):
        adapter = _make_adapter()

        async def fake_cli(args, *, timeout=600.0):
            assert args[:3] == ["generate", "cost", "nano_banana_pro"]
            assert "--image-references" in args
            return 0, "2 credits\n", ""

        adapter._run_higgsfield_cli = fake_cli
        with patch("plugins.platforms.mattermost.adapter.asyncio.create_task") as create_task:
            handled = await adapter._maybe_start_higgsfield_image_edit(
                channel_id="image_channel",
                sender_id="user_1",
                post_id="post_1",
                message_text="옷만 베이지 정장으로",
                media_urls=["C:/tmp/input.png"],
                media_types=["image/png"],
                force=True,
            )

        assert handled is True
        create_task.assert_called_once()
        create_task.call_args.args[0].close()

    def test_detects_higgsfield_image_edit_channel_from_env(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "chan_a, chan_b")
        assert adapter._is_higgsfield_image_edit_channel("chan_b") is True
        assert adapter._is_higgsfield_image_edit_channel("chan_c") is False

    def test_all_image_edit_channels_are_archive_channels(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_ARCHIVE_CHANNELS", "archive_chan")
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "chan_a, chan_b")

        assert adapter._higgsfield_image_archive_channels() == [
            "archive_chan",
            "chan_a",
            "chan_b",
        ]

    def test_detects_followup_image_edit_text(self):
        from plugins.platforms.mattermost.adapter import (
            _extract_higgsfield_history_offset,
            _extract_higgsfield_text_image_generation_prompt,
            _is_higgsfield_image_edit_end_command,
            _is_higgsfield_history_list_command,
            _is_higgsfield_new_image_edit_command,
            _is_higgsfield_text_image_generation_request,
            _looks_like_higgsfield_followup_edit_request,
            _looks_like_higgsfield_image_edit_request,
        )

        assert _looks_like_higgsfield_followup_edit_request("색감 더 따뜻하게 수정해줘") is True
        assert _looks_like_higgsfield_followup_edit_request("모핑으로 이어지는 경계가 어색하니 더 자연스럽게") is True
        assert _looks_like_higgsfield_followup_edit_request("이미지 편집") is False
        assert _looks_like_higgsfield_image_edit_request("이미지 편집", has_image=True) is False
        assert _looks_like_higgsfield_followup_edit_request("안녕하세요") is False
        assert _is_higgsfield_image_edit_end_command("이미지 편집 종료") is True
        assert _is_higgsfield_new_image_edit_command("새 이미지 편집") is True
        assert _is_higgsfield_history_list_command("이미지 편집 목록") is True
        assert _extract_higgsfield_history_offset("직전 이미지에서 배경 수정") == 0
        assert _extract_higgsfield_history_offset("2번 이미지에서 배경 수정") == 1
        assert _extract_higgsfield_history_offset("두 번 전 이미지에서 배경 수정") == 1
        assert _extract_higgsfield_history_offset("이전 이전 이미지에서 배경 수정") == 1
        assert _extract_higgsfield_history_offset("3번째 이전 결과에서 윤곽선 수정") == 2
        assert _is_higgsfield_text_image_generation_request("이미지 생성 고양이 캐릭터") is True
        assert _is_higgsfield_text_image_generation_request("새 이미지 생성 미니멀 포스터") is True
        assert _extract_higgsfield_text_image_generation_prompt("이미지 생성 고양이 캐릭터") == "고양이 캐릭터"

    @pytest.mark.asyncio
    async def test_image_edit_end_command_disables_followup(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "image_channel")
        adapter.send = AsyncMock()

        handled = await adapter._maybe_handle_higgsfield_image_edit_control(
            channel_id="image_channel",
            sender_id="user_1",
            message_text="이미지 편집 종료",
            has_image=False,
        )

        assert handled is True
        assert "image_channel:user_1" in adapter._higgsfield_followup_disabled
        adapter.send.assert_awaited_once()

        adapter.send.reset_mock()
        handled = await adapter._maybe_start_higgsfield_image_edit(
            channel_id="image_channel",
            sender_id="user_1",
            post_id="post_followup",
            message_text="색감 더 따뜻하게 수정해줘",
            media_urls=[],
            media_types=[],
        )

        assert handled is True
        assert "종료된 상태" in adapter.send.call_args.args[1]

    @pytest.mark.asyncio
    async def test_new_image_edit_command_without_image_asks_for_attachment(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "image_channel")
        adapter.send = AsyncMock()

        handled = await adapter._maybe_handle_higgsfield_image_edit_control(
            channel_id="image_channel",
            sender_id="user_1",
            message_text="새 이미지 편집",
            has_image=False,
        )

        assert handled is True
        assert "원본 이미지를 첨부" in adapter.send.call_args.args[1]

    @pytest.mark.asyncio
    async def test_text_only_image_generation_starts_higgsfield_job(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "image_channel")

        async def fake_cli(args, *, timeout=600.0):
            assert args[:3] == ["generate", "cost", "gpt_image_2"]
            assert "--image-references" not in args
            assert args[args.index("--prompt") + 1] == "흰 배경의 미니멀한 화장품 광고 이미지"
            return 0, "3 credits\n", ""

        adapter._run_higgsfield_cli = fake_cli
        adapter.send = AsyncMock()

        with patch("plugins.platforms.mattermost.adapter.asyncio.create_task") as create_task:
            handled = await adapter._maybe_start_higgsfield_image_edit(
                channel_id="image_channel",
                sender_id="user_1",
                post_id="post_generate",
                message_text="이미지 생성 흰 배경의 미니멀한 화장품 광고 이미지",
                media_urls=[],
                media_types=[],
            )

        assert handled is True
        adapter.send.assert_not_called()
        pending = create_task.call_args.args[0].cr_frame.f_locals["pending"]
        assert pending["mode"] == "generate"
        assert pending["job_type"] == "gpt_image_2"
        assert pending["image_path"] == ""
        create_task.call_args.args[0].close()

    @pytest.mark.asyncio
    async def test_text_only_image_generation_requires_prompt(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "image_channel")
        adapter.send = AsyncMock()

        handled = await adapter._maybe_start_higgsfield_image_edit(
            channel_id="image_channel",
            sender_id="user_1",
            post_id="post_generate",
            message_text="새 이미지 생성",
            media_urls=[],
            media_types=[],
        )

        assert handled is True
        assert "생성할 이미지 설명" in adapter.send.call_args.args[1]

    @pytest.mark.asyncio
    async def test_followup_without_attachment_uses_latest_archived_result_url(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "image_channel")
        archive_message = (
            "계속 수정용 Higgsfield 이미지 기록\n"
            "HERMES_HIGGSFIELD_IMAGE_RESULT\n"
            "```json\n"
            + json.dumps({
                "source": "hermes_higgsfield_edit",
                "origin_channel_id": "image_channel",
                "user_id": "user_1",
                "result_job_id": "0a3144b3-985f-43b1-84b6-f2564896b854",
                "result_url": "https://cdn.example.com/result.png",
            })
            + "\n```"
        )

        async def fake_api_get(path):
            assert path == "channels/image_channel/posts?page=0&per_page=80"
            return {
                "order": ["archive_post_1"],
                "posts": {"archive_post_1": {"message": archive_message}},
            }

        async def fake_cli(args, *, timeout=600.0):
            assert args[:3] == ["generate", "cost", "nano_banana_pro"]
            assert "--image-references" in args
            assert "C:/cache/result.png" in args
            return 0, "2 credits\n", ""

        adapter._api_get = fake_api_get
        adapter._cache_remote_higgsfield_image_reference = AsyncMock(return_value="C:/cache/result.png")
        adapter._run_higgsfield_cli = fake_cli
        adapter.send = AsyncMock()

        with patch("plugins.platforms.mattermost.adapter.asyncio.create_task") as create_task:
            handled = await adapter._maybe_start_higgsfield_image_edit(
                channel_id="image_channel",
                sender_id="user_1",
                post_id="post_followup",
                message_text="색감 더 따뜻하게 수정해줘",
                media_urls=[],
                media_types=[],
            )

        assert handled is True
        adapter.send.assert_not_called()
        create_task.assert_called_once()
        pending = create_task.call_args.args[0].cr_frame.f_locals["pending"]
        adapter._cache_remote_higgsfield_image_reference.assert_awaited_once_with(
            "https://cdn.example.com/result.png"
        )
        assert pending["image_path"] == "C:/cache/result.png"
        create_task.call_args.args[0].close()

    @pytest.mark.asyncio
    async def test_followup_can_target_second_previous_archived_result(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "image_channel")

        def archive_message(url: str) -> str:
            return (
                "계속 수정용 Higgsfield 이미지 기록\n"
                "HERMES_HIGGSFIELD_IMAGE_RESULT\n"
                "```json\n"
                + json.dumps({
                    "source": "hermes_higgsfield_edit",
                    "origin_channel_id": "image_channel",
                    "user_id": "user_1",
                    "result_url": url,
                })
                + "\n```"
            )

        async def fake_api_get(path):
            assert path == "channels/image_channel/posts?page=0&per_page=80"
            return {
                "order": ["archive_latest", "archive_previous"],
                "posts": {
                    "archive_latest": {"message": archive_message("https://cdn.example.com/latest.png")},
                    "archive_previous": {"message": archive_message("https://cdn.example.com/previous.png")},
                },
            }

        async def fake_cache(url):
            return {
                "https://cdn.example.com/latest.png": "C:/cache/latest.png",
                "https://cdn.example.com/previous.png": "C:/cache/previous.png",
            }[url]

        async def fake_cli(args, *, timeout=600.0):
            assert args[:3] == ["generate", "cost", "nano_banana_pro"]
            assert args[args.index("--image-references") + 1] == "C:/cache/previous.png"
            return 0, "2 credits\n", ""

        adapter._api_get = fake_api_get
        adapter._cache_remote_higgsfield_image_reference = fake_cache
        adapter._run_higgsfield_cli = fake_cli
        adapter.send = AsyncMock()

        with patch("plugins.platforms.mattermost.adapter.asyncio.create_task") as create_task:
            handled = await adapter._maybe_start_higgsfield_image_edit(
                channel_id="image_channel",
                sender_id="user_1",
                post_id="post_followup",
                message_text="두 번 전 이미지에서 배경만 흰색으로 바꿔줘",
                media_urls=[],
                media_types=[],
            )

        assert handled is True
        pending = create_task.call_args.args[0].cr_frame.f_locals["pending"]
        assert pending["image_path"] == "C:/cache/previous.png"
        create_task.call_args.args[0].close()

    @pytest.mark.asyncio
    async def test_history_list_command_shows_numbered_results(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "image_channel")

        def archive_message(job_type: str) -> str:
            return (
                "계속 수정용 Higgsfield 이미지 기록\n"
                "HERMES_HIGGSFIELD_IMAGE_RESULT\n"
                "```json\n"
                + json.dumps({
                    "source": "hermes_higgsfield_edit",
                    "origin_channel_id": "image_channel",
                    "user_id": "user_1",
                    "job_type": job_type,
                    "result_url": f"https://cdn.example.com/{job_type}.png",
                })
                + "\n```"
            )

        async def fake_api_get(path):
            return {
                "order": ["archive_1", "archive_2"],
                "posts": {
                    "archive_1": {"message": archive_message("nano_banana_pro"), "create_at": 1000},
                    "archive_2": {"message": archive_message("gpt_image_2"), "create_at": 2000},
                },
            }

        adapter._api_get = fake_api_get
        adapter.send = AsyncMock()

        handled = await adapter._maybe_handle_higgsfield_image_edit_control(
            channel_id="image_channel",
            sender_id="user_1",
            message_text="이미지 편집 목록",
            has_image=False,
        )

        assert handled is True
        message = adapter.send.call_args.args[1]
        assert "1. 직전 결과" in message
        assert "2. 2번 결과" in message
        assert "2번 이미지에서" in message

    @pytest.mark.asyncio
    async def test_followup_uses_mattermost_result_file_when_result_url_unavailable(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "image_channel")
        archive_message = (
            "계속 수정용 Higgsfield 이미지 기록\n"
            "HERMES_HIGGSFIELD_IMAGE_RESULT\n"
            "```json\n"
            + json.dumps({
                "source": "hermes_higgsfield_edit",
                "origin_channel_id": "image_channel",
                "user_id": "user_1",
                "result_job_id": "0a3144b3-985f-43b1-84b6-f2564896b854",
                "result_url": "https://cdn.example.com/expired.png",
            })
            + "\n```"
        )

        async def fake_api_get(path):
            assert path == "channels/image_channel/posts?page=0&per_page=80"
            return {
                "order": ["archive_post_1", "result_post_1"],
                "posts": {
                    "archive_post_1": {"message": archive_message},
                    "result_post_1": {
                        "message": "Higgsfield 이미지 편집이 완료되었습니다.",
                        "file_ids": ["file_1"],
                    },
                },
            }

        async def fake_cli(args, *, timeout=600.0):
            assert args[:3] == ["generate", "cost", "nano_banana_pro"]
            assert args[args.index("--image-references") + 1] == "C:/cache/uploaded.png"
            return 0, "2 credits\n", ""

        adapter._api_get = fake_api_get
        adapter._cache_remote_higgsfield_image_reference = AsyncMock(return_value="")
        adapter._cache_mattermost_file_image_reference = AsyncMock(return_value="C:/cache/uploaded.png")
        adapter._run_higgsfield_cli = fake_cli
        adapter.send = AsyncMock()

        with patch("plugins.platforms.mattermost.adapter.asyncio.create_task") as create_task:
            handled = await adapter._maybe_start_higgsfield_image_edit(
                channel_id="image_channel",
                sender_id="user_1",
                post_id="post_followup",
                message_text="색감을 수정하지마. 색감 그대로 윤곽선만 수정해",
                media_urls=[],
                media_types=[],
            )

        assert handled is True
        create_task.assert_called_once()
        pending = create_task.call_args.args[0].cr_frame.f_locals["pending"]
        assert pending["image_path"] == "C:/cache/uploaded.png"
        adapter._cache_mattermost_file_image_reference.assert_awaited_once_with("file_1")
        create_task.call_args.args[0].close()

    @pytest.mark.asyncio
    async def test_followup_does_not_use_job_id_as_image_reference(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "image_channel")
        archive_message = (
            "계속 수정용 Higgsfield 이미지 기록\n"
            "HERMES_HIGGSFIELD_IMAGE_RESULT\n"
            "```json\n"
            + json.dumps({
                "source": "hermes_higgsfield_edit",
                "origin_channel_id": "image_channel",
                "user_id": "user_1",
                "result_job_id": "0a3144b3-985f-43b1-84b6-f2564896b854",
                "result_url": "https://cdn.example.com/expired.png",
            })
            + "\n```"
        )

        async def fake_api_get(path):
            return {
                "order": ["archive_post_1"],
                "posts": {"archive_post_1": {"message": archive_message}},
            }

        adapter._api_get = fake_api_get
        adapter._cache_remote_higgsfield_image_reference = AsyncMock(return_value="")
        adapter.send = AsyncMock()

        handled = await adapter._maybe_start_higgsfield_image_edit(
            channel_id="image_channel",
            sender_id="user_1",
            post_id="post_followup",
            message_text="색감을 수정하지마. 색감 그대로 윤곽선만 수정해",
            media_urls=[],
            media_types=[],
        )

        assert handled is True
        adapter.send.assert_awaited_once()
        assert "먼저 편집할 이미지를 첨부" in adapter.send.call_args.args[1]

    @pytest.mark.asyncio
    async def test_followup_without_archive_asks_for_image(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HIGGSFIELD_IMAGE_EDIT_CHANNELS", "image_channel")

        async def fake_api_get(path):
            return {"order": [], "posts": {}}

        adapter._api_get = fake_api_get
        adapter.send = AsyncMock()

        handled = await adapter._maybe_start_higgsfield_image_edit(
            channel_id="image_channel",
            sender_id="user_1",
            post_id="post_followup",
            message_text="색감 더 따뜻하게 수정해줘",
            media_urls=[],
            media_types=[],
        )

        assert handled is True
        adapter.send.assert_awaited_once()
        assert "먼저 편집할 이미지를 첨부" in adapter.send.call_args.args[1]

    @pytest.mark.asyncio
    async def test_recraft_logo_request_omits_media_inputs(self):
        adapter = _make_adapter()

        async def fake_cli(args, *, timeout=600.0):
            assert args[:3] == ["generate", "cost", "recraft_v4_1"]
            assert "--image-references" not in args
            assert "--image" not in args
            assert args[args.index("--model_type") + 1] == "vector"
            prompt = args[args.index("--prompt") + 1]
            assert "Do not assume there is an attached image reference" in prompt
            return 0, "1 credit\n", ""

        adapter._run_higgsfield_cli = fake_cli

        with patch("plugins.platforms.mattermost.adapter.asyncio.create_task") as create_task:
            handled = await adapter._maybe_start_higgsfield_image_edit(
                channel_id="channel_1",
                sender_id="user_1",
                post_id="post_1",
                message_text="Recraft V4.1 요청: 로고 제작",
                media_urls=["C:/tmp/ignored-reference.png"],
                media_types=["image/png"],
            )

        assert handled is True
        create_task.assert_called_once()
        create_task.call_args.args[0].close()

    def test_extracts_result_url_from_dict_json(self):
        from plugins.platforms.mattermost.adapter import _extract_higgsfield_result_url

        output = json.dumps({"result_url": "https://cdn.example.com/result.png"})
        assert _extract_higgsfield_result_url(output) == "https://cdn.example.com/result.png"

    def test_background_edit_prompt_does_not_preserve_old_background(self):
        from plugins.platforms.mattermost.adapter import _build_higgsfield_edit_prompt

        prompt = _build_higgsfield_edit_prompt("배경 제거하고 흰색으로 바꿔줘")

        assert "do not preserve the old background" in prompt
        assert "clean pure white background" in prompt
        assert "leftover background" in prompt
        preserve_clause = prompt.split("Preserve all non-target attributes", 1)[1]
        assert "background" not in preserve_clause

    def test_lighting_edit_prompt_does_not_preserve_old_lighting(self):
        from plugins.platforms.mattermost.adapter import _build_higgsfield_edit_prompt

        prompt = _build_higgsfield_edit_prompt("조명을 더 밝고 따뜻하게 바꿔줘")

        assert "Change only the requested color, tone, brightness" in prompt
        preserve_clause = prompt.split("Preserve all non-target attributes", 1)[1]
        assert "lighting" not in preserve_clause
        assert "background" in preserve_clause

    def test_pose_edit_prompt_does_not_preserve_old_pose(self):
        from plugins.platforms.mattermost.adapter import _build_higgsfield_edit_prompt

        prompt = _build_higgsfield_edit_prompt("카메라 각도랑 포즈를 살짝 바꿔줘")

        assert "Change only the requested pose" in prompt
        preserve_clause = prompt.split("Preserve all non-target attributes", 1)[1]
        assert "pose" not in preserve_clause
        assert "camera angle" not in preserve_clause
        assert "identity" in preserve_clause

    def test_edge_edit_prompt_preserves_background_and_colors(self):
        from plugins.platforms.mattermost.adapter import _build_higgsfield_edit_prompt

        prompt = _build_higgsfield_edit_prompt("색감 그대로 윤곽선만 자연스럽게 수정해")

        assert "Refine only the requested edges" in prompt
        assert "Preserve colors, background" in prompt
        preserve_clause = prompt.split("Preserve all non-target attributes", 1)[1]
        assert "background" in preserve_clause

    def test_extracts_job_id_from_plain_uuid(self):
        from plugins.platforms.mattermost.adapter import _extract_higgsfield_job_id

        assert _extract_higgsfield_job_id("0a3144b3-985f-43b1-84b6-f2564896b854\n") == "0a3144b3-985f-43b1-84b6-f2564896b854"

    @pytest.mark.asyncio
    async def test_uuid_output_fetches_job_before_sending_image(self):
        adapter = _make_adapter()
        pending = {
            "job_type": "nano_banana_pro",
            "prompt": "edit",
            "image_path": "C:/tmp/input.png",
            "aspect": "1:1",
            "resolution": "1k",
            "cost": "2 credits",
        }

        async def fake_cli(args, *, timeout=600.0):
            if args[:3] == ["generate", "create", "nano_banana_pro"]:
                assert "--image-references" in args
                assert "--image" not in args
                assert "C:/tmp/input.png" in args
                return 0, "0a3144b3-985f-43b1-84b6-f2564896b854\n", ""
            if args == ["generate", "get", "0a3144b3-985f-43b1-84b6-f2564896b854", "--json"]:
                return 0, json.dumps({"result_url": "https://cdn.example.com/result.png"}), ""
            raise AssertionError(args)

        adapter._run_higgsfield_cli = fake_cli
        adapter.send = AsyncMock()
        adapter.send_image = AsyncMock()

        await adapter._run_pending_higgsfield_edit("channel_1", "", pending)

        adapter.send.assert_not_called()
        adapter.send_image.assert_awaited_once()
        args, kwargs = adapter.send_image.call_args
        assert args[:2] == ("channel_1", "https://cdn.example.com/result.png")
        assert "Higgsfield 이미지 편집이 완료되었습니다" in kwargs["caption"]
        assert "https://cdn.example.com/result.png" not in kwargs["caption"]

    @pytest.mark.asyncio
    async def test_recraft_create_uses_vector_without_media(self):
        adapter = _make_adapter()
        pending = {
            "job_type": "recraft_v4_1",
            "prompt": "logo",
            "image_path": "",
            "aspect": "1:1",
            "resolution": "2k",
            "cost": "1 credit",
        }

        async def fake_cli(args, *, timeout=600.0):
            if args[:3] == ["generate", "create", "recraft_v4_1"]:
                assert "--image-references" not in args
                assert "--image" not in args
                assert args[args.index("--model_type") + 1] == "vector"
                assert args[args.index("--resolution") + 1] == "2k"
                return 0, json.dumps({"result_url": "https://cdn.example.com/logo.png"}), ""
            raise AssertionError(args)

        adapter._run_higgsfield_cli = fake_cli
        adapter.send = AsyncMock()
        adapter.send_image = AsyncMock()

        await adapter._run_pending_higgsfield_edit("channel_1", "", pending)

        adapter.send.assert_not_called()
        adapter.send_image.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uuid_lookup_retries_until_result_url_is_available(self):
        adapter = _make_adapter()
        calls = 0

        async def fake_cli(args, *, timeout=600.0):
            nonlocal calls
            assert args == ["generate", "get", "0a3144b3-985f-43b1-84b6-f2564896b854", "--json"]
            calls += 1
            if calls == 1:
                return 0, json.dumps({"status": "completed"}), ""
            return 0, json.dumps({"result_url": "https://cdn.example.com/result.png"}), ""

        adapter._run_higgsfield_cli = fake_cli

        with patch("plugins.platforms.mattermost.adapter.asyncio.sleep", new=AsyncMock()) as sleep:
            url = await adapter._resolve_higgsfield_result_url("0a3144b3-985f-43b1-84b6-f2564896b854")

        assert url == "https://cdn.example.com/result.png"
        assert calls == 2
        sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_higgsfield_cli_runs_hidden_on_windows(self):
        adapter = _make_adapter()

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"ok", b""

        with patch("plugins.platforms.mattermost.adapter.shutil.which", return_value="higgsfield.CMD"), \
             patch("plugins.platforms.mattermost.adapter.os.name", "nt"), \
             patch("plugins.platforms.mattermost.adapter.asyncio.create_subprocess_exec", new=AsyncMock(return_value=FakeProc())) as create_proc:
            code, stdout, stderr = await adapter._run_higgsfield_cli(["--version"])

        assert (code, stdout, stderr) == (0, "ok", "")
        assert create_proc.call_args.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW


class TestMattermostTruncateMessage:
    def setup_method(self):
        self.adapter = _make_adapter()

    def test_short_message_single_chunk(self):
        msg = "Hello, world!"
        chunks = self.adapter.truncate_message(msg, 4000)
        assert len(chunks) == 1
        assert chunks[0] == msg

    def test_long_message_splits(self):
        msg = "a " * 2500  # 5000 chars
        chunks = self.adapter.truncate_message(msg, 4000)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 4000

    def test_custom_max_length(self):
        msg = "Hello " * 20
        chunks = self.adapter.truncate_message(msg, max_length=50)
        assert all(len(c) <= 50 for c in chunks)

    def test_exactly_at_limit(self):
        msg = "x" * 4000
        chunks = self.adapter.truncate_message(msg, 4000)
        assert len(chunks) == 1


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

class TestMattermostSend:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._session = MagicMock()

    @pytest.mark.asyncio
    async def test_send_calls_api_post(self):
        """send() should POST to /api/v4/posts with channel_id and message."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post123"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send("channel_1", "Hello!")

        assert result.success is True
        assert result.message_id == "post123"

        # Verify post was called with correct URL
        call_args = self.adapter._session.post.call_args
        assert "/api/v4/posts" in call_args[0][0]
        # Verify payload
        payload = call_args[1]["json"]
        assert payload["channel_id"] == "channel_1"
        assert payload["message"] == "Hello!"

    @pytest.mark.asyncio
    async def test_send_empty_content_succeeds(self):
        """Empty content should return success without calling the API."""
        result = await self.adapter.send("channel_1", "")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_with_thread_reply(self):
        """When reply_mode is 'thread', reply_to should become root_id."""
        self.adapter._reply_mode = "thread"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post456"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        # send() now calls _resolve_root_id → _api_get("posts/<id>") first
        # to make sure root_id points to a thread root, so we need to mock
        # the GET too.  Return an empty dict (no root_id) so the resolver
        # falls back to the original reply_to as the root.
        mock_get_resp = AsyncMock()
        mock_get_resp.status = 200
        mock_get_resp.json = AsyncMock(return_value={"id": "root_post", "root_id": ""})
        mock_get_resp.text = AsyncMock(return_value="")
        mock_get_resp.__aenter__ = AsyncMock(return_value=mock_get_resp)
        mock_get_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)
        self.adapter._session.get = MagicMock(return_value=mock_get_resp)

        result = await self.adapter.send("channel_1", "Reply!", reply_to="root_post")

        assert result.success is True
        payload = self.adapter._session.post.call_args[1]["json"]
        assert payload["root_id"] == "root_post"

    @pytest.mark.asyncio
    async def test_send_without_thread_no_root_id(self):
        """When reply_mode is 'off', reply_to should NOT set root_id."""
        self.adapter._reply_mode = "off"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post789"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send("channel_1", "Reply!", reply_to="root_post")

        assert result.success is True
        payload = self.adapter._session.post.call_args[1]["json"]
        assert "root_id" not in payload


    @pytest.mark.asyncio
    async def test_send_uses_metadata_thread_id_for_progress_messages(self):
        """Progress/status messages pass Mattermost thread context via metadata."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "root_post_123", "root_id": ""})
        self.adapter._api_post = AsyncMock(return_value={"id": "progress_post"})

        result = await self.adapter.send(
            "channel_1",
            "⚡ terminal...",
            metadata={"thread_id": "root_post_123"},
        )

        assert result.success is True
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "root_post_123"

    @pytest.mark.asyncio
    async def test_progress_send_with_invalid_thread_root_never_falls_back_flat(self):
        """Tool/status/progress bubbles must stay quiet when the thread is broken."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._last_post_status = 400
        self.adapter._last_post_error = "api.context.invalid_param.app_error: invalid root_id"
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter.send(
            "channel_1",
            "⚙️ terminal...",
            metadata={"thread_id": "bad_root"},
        )

        assert result.success is False
        assert self.adapter._api_post.call_count == 1
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "bad_root"

    @pytest.mark.asyncio
    async def test_notify_send_with_invalid_thread_root_falls_back_flat_with_warning(self):
        """Notify-worthy replies may fall back flat so the answer is not lost."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._last_post_status = 400
        self.adapter._last_post_error = "api.context.invalid_param.app_error: invalid root_id"
        self.adapter._api_post = AsyncMock(side_effect=[{}, {"id": "flat_final"}])

        result = await self.adapter.send(
            "channel_1",
            "Final answer body",
            reply_to="bad_root",
            metadata={"notify": True},
        )

        assert result.success is True
        assert result.message_id == "flat_final"
        assert self.adapter._api_post.call_count == 2
        threaded_payload = self.adapter._api_post.call_args_list[0][0][1]
        flat_payload = self.adapter._api_post.call_args_list[1][0][1]
        assert threaded_payload["root_id"] == "bad_root"
        assert "root_id" not in flat_payload
        assert flat_payload["channel_id"] == "channel_1"
        assert "Mattermost thread delivery failed" in flat_payload["message"]
        assert "Final answer body" in flat_payload["message"]

    @pytest.mark.asyncio
    async def test_notify_send_with_server_error_does_not_fall_back_flat(self):
        """Notify fallback is only for broken thread roots, not generic API failures."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "root_post", "root_id": ""})
        self.adapter._last_post_status = 500
        self.adapter._last_post_error = "Internal Server Error"
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter.send(
            "channel_1",
            "Final answer body",
            reply_to="root_post",
            metadata={"notify": True},
        )

        assert result.success is False
        assert self.adapter._api_post.call_count == 1
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "root_post"

    @pytest.mark.asyncio
    async def test_progress_send_with_invalid_thread_root_never_falls_back_flat(self):
        """Tool/status/progress bubbles must stay quiet when the thread is broken."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter.send(
            "channel_1",
            "⚙️ terminal...",
            metadata={"thread_id": "bad_root"},
        )

        assert result.success is False
        assert self.adapter._api_post.call_count == 1
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "bad_root"

    @pytest.mark.asyncio
    async def test_send_api_failure(self):
        """When API returns error, send should return failure."""
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.json = AsyncMock(return_value={})
        mock_resp.text = AsyncMock(return_value="Internal Server Error")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send("channel_1", "Hello!")

        assert result.success is False


# ---------------------------------------------------------------------------
# WebSocket event parsing
# ---------------------------------------------------------------------------

class TestMattermostWebSocketParsing:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter._bot_username = "hermes-bot"
        # Mock handle_message to capture the MessageEvent without processing
        self.adapter.handle_message = AsyncMock()

    @pytest.mark.asyncio
    async def test_parse_posted_event(self):
        """'posted' events should extract message from double-encoded post JSON."""
        post_data = {
            "id": "post_abc",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id Hello from Matrix!",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),  # double-encoded JSON string
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        # @mention is stripped from the message text
        assert msg_event.text == "Hello from Matrix!"
        assert msg_event.message_id == "post_abc"

    @pytest.mark.asyncio
    async def test_higgsfield_model_help_request_sends_recommendations(self):
        post_data = {
            "id": "post_models",
            "user_id": "user_123",
            "channel_id": "chan_dm",
            "message": "힉스필드모델",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "D",
            },
        }
        self.adapter.send = AsyncMock()

        await self.adapter._handle_ws_event(event)

        self.adapter.handle_message.assert_not_called()
        self.adapter.send.assert_awaited_once()
        sent_text = self.adapter.send.call_args.args[1]
        assert "GPT Image 2" in sent_text
        assert "Recraft V4.1" in sent_text
        assert "Seedance 2.0" in sent_text

    @pytest.mark.asyncio
    async def test_ignore_own_messages(self):
        """Messages from the bot's own user_id should be ignored."""
        post_data = {
            "id": "post_self",
            "user_id": "bot_user_id",  # same as bot
            "channel_id": "chan_456",
            "message": "Bot echo",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_ignore_non_posted_events(self):
        """Non-'posted' events should be ignored."""
        event = {
            "event": "typing",
            "data": {"user_id": "user_123"},
        }

        await self.adapter._handle_ws_event(event)
        assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_ignore_system_posts(self):
        """Posts with a 'type' field (system messages) should be ignored."""
        post_data = {
            "id": "sys_post",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "user joined",
            "type": "system_join_channel",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_channel_type_mapping(self):
        """channel_type 'D' should map to 'dm'."""
        post_data = {
            "id": "post_dm",
            "user_id": "user_123",
            "channel_id": "chan_dm",
            "message": "DM message",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "D",
                "sender_name": "@bob",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_leading_space_slash_command_is_command(self):
        """Mattermost mobile suggests leading-space slash commands."""
        post_data = {
            "id": "post_cmd",
            "user_id": "user_123",
            "channel_id": "chan_dm",
            "message": " /new",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "D",
                "sender_name": "@bob",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "/new"
        assert msg_event.message_type is MessageType.COMMAND
        assert msg_event.get_command() == "new"

    @pytest.mark.asyncio
    async def test_leading_space_normal_text_is_preserved(self):
        """Only command-shaped mobile messages should be normalized."""
        post_data = {
            "id": "post_text",
            "user_id": "user_123",
            "channel_id": "chan_dm",
            "message": " hello",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "D",
                "sender_name": "@bob",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == " hello"
        assert msg_event.message_type is MessageType.TEXT

    @pytest.mark.asyncio
    async def test_thread_id_from_root_id(self):
        """Post with root_id should have thread_id set."""
        post_data = {
            "id": "post_reply",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id Thread reply",
            "root_id": "root_post_123",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.source.thread_id == "root_post_123"

    @pytest.mark.asyncio
    async def test_invalid_post_json_ignored(self):
        """Invalid JSON in data.post should be silently ignored."""
        event = {
            "event": "posted",
            "data": {
                "post": "not-valid-json{{{",
                "channel_type": "O",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert not self.adapter.handle_message.called


# ---------------------------------------------------------------------------
# Mention behavior (require_mention + free_response_channels)
# ---------------------------------------------------------------------------

class TestMattermostMentionBehavior:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter._bot_username = "hermes-bot"
        self.adapter.handle_message = AsyncMock()

    def _make_event(self, message, channel_type="O", channel_id="chan_456"):
        post_data = {
            "id": "post_mention",
            "user_id": "user_123",
            "channel_id": channel_id,
            "message": message,
        }
        return {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": channel_type,
                "sender_name": "@alice",
            },
        }

    @pytest.mark.asyncio
    async def test_require_mention_true_skips_without_mention(self):
        """Default: messages without @mention in channels are skipped."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            os.environ.pop("MATTERMOST_FREE_RESPONSE_CHANNELS", None)
            await self.adapter._handle_ws_event(self._make_event("hello"))
            assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_require_mention_false_responds_to_all(self):
        """MATTERMOST_REQUIRE_MENTION=false: respond to all channel messages."""
        with patch.dict(os.environ, {"MATTERMOST_REQUIRE_MENTION": "false"}):
            await self.adapter._handle_ws_event(self._make_event("hello"))
            assert self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_free_response_channel_responds_without_mention(self):
        """Messages in free-response channels don't need @mention."""
        with patch.dict(os.environ, {"MATTERMOST_FREE_RESPONSE_CHANNELS": "chan_456,chan_789"}):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(self._make_event("hello", channel_id="chan_456"))
            assert self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_non_free_channel_still_requires_mention(self):
        """Channels NOT in free-response list still require @mention."""
        with patch.dict(os.environ, {"MATTERMOST_FREE_RESPONSE_CHANNELS": "chan_789"}):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(self._make_event("hello", channel_id="chan_456"))
            assert not self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_dm_always_responds(self):
        """DMs (channel_type=D) always respond regardless of mention settings."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(self._make_event("hello", channel_type="D"))
            assert self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_mention_stripped_from_text(self):
        """@mention is stripped from message text."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(
                self._make_event("@hermes-bot what is 2+2")
            )
            assert self.adapter.handle_message.called
            msg = self.adapter.handle_message.call_args[0][0]
            assert "@hermes-bot" not in msg.text
            assert "2+2" in msg.text


# ---------------------------------------------------------------------------
# File upload (send_image)
# ---------------------------------------------------------------------------

class TestMattermostFileUpload:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._session = MagicMock()

    @pytest.mark.asyncio
    @patch("tools.url_safety.is_safe_url", return_value=True)
    async def test_send_image_downloads_and_uploads(self, _mock_safe):
        """send_image should download the URL, upload via /api/v4/files, then post."""
        # Mock the download (GET)
        mock_dl_resp = AsyncMock()
        mock_dl_resp.status = 200
        mock_dl_resp.read = AsyncMock(return_value=b"\x89PNG\x00fake-image-data")
        mock_dl_resp.content_type = "image/png"
        mock_dl_resp.__aenter__ = AsyncMock(return_value=mock_dl_resp)
        mock_dl_resp.__aexit__ = AsyncMock(return_value=False)

        # Mock the upload (POST to /files)
        mock_upload_resp = AsyncMock()
        mock_upload_resp.status = 200
        mock_upload_resp.json = AsyncMock(return_value={
            "file_infos": [{"id": "file_abc123"}]
        })
        mock_upload_resp.text = AsyncMock(return_value="")
        mock_upload_resp.__aenter__ = AsyncMock(return_value=mock_upload_resp)
        mock_upload_resp.__aexit__ = AsyncMock(return_value=False)

        # Mock the post (POST to /posts)
        mock_post_resp = AsyncMock()
        mock_post_resp.status = 200
        mock_post_resp.json = AsyncMock(return_value={"id": "post_with_file"})
        mock_post_resp.text = AsyncMock(return_value="")
        mock_post_resp.__aenter__ = AsyncMock(return_value=mock_post_resp)
        mock_post_resp.__aexit__ = AsyncMock(return_value=False)

        # Route calls: first GET (download), then POST (upload), then POST (create post)
        self.adapter._session.get = MagicMock(return_value=mock_dl_resp)
        post_call_count = 0
        original_post_returns = [mock_upload_resp, mock_post_resp]

        def post_side_effect(*args, **kwargs):
            nonlocal post_call_count
            resp = original_post_returns[min(post_call_count, len(original_post_returns) - 1)]
            post_call_count += 1
            return resp

        self.adapter._session.post = MagicMock(side_effect=post_side_effect)

        result = await self.adapter.send_image(
            "channel_1", "https://img.example.com/cat.png", caption="A cat"
        )

        assert result.success is True
        assert result.message_id == "post_with_file"


# ---------------------------------------------------------------------------
# Dedup cache
# ---------------------------------------------------------------------------

class TestMattermostDedup:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        # Mock handle_message to capture calls without processing
        self.adapter.handle_message = AsyncMock()

    @pytest.mark.asyncio
    async def test_duplicate_post_ignored(self):
        """The same post_id within the TTL window should be ignored."""
        post_data = {
            "id": "post_dup",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id Hello!",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

        # First time: should process
        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.call_count == 1

        # Second time (same post_id): should be deduped
        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.call_count == 1  # still 1

    @pytest.mark.asyncio
    async def test_different_post_ids_both_processed(self):
        """Different post IDs should both be processed."""
        for i, pid in enumerate(["post_a", "post_b"]):
            post_data = {
                "id": pid,
                "user_id": "user_123",
                "channel_id": "chan_456",
                "message": f"@bot_user_id Message {i}",
            }
            event = {
                "event": "posted",
                "data": {
                    "post": json.dumps(post_data),
                    "channel_type": "O",
                    "sender_name": "@alice",
                },
            }
            await self.adapter._handle_ws_event(event)

        assert self.adapter.handle_message.call_count == 2

    def test_prune_seen_clears_expired(self):
        """Dedup cache should remove entries older than TTL on overflow."""
        now = time.time()
        dedup = self.adapter._dedup
        # Fill with enough expired entries to trigger pruning
        for i in range(dedup._max_size + 10):
            dedup._seen[f"old_{i}"] = now - 600  # 10 min ago (older than default TTL)

        # Add a fresh one
        dedup._seen["fresh"] = now

        # Trigger pruning by calling is_duplicate with a new entry (over max_size)
        dedup.is_duplicate("trigger_prune")

        # Old entries should be pruned, fresh one kept
        assert "fresh" in dedup._seen
        assert len(dedup._seen) < dedup._max_size + 10

    def test_seen_cache_tracks_post_ids(self):
        """Posts are tracked in the dedup cache."""
        self.adapter._dedup._seen["test_post"] = time.time()
        assert "test_post" in self.adapter._dedup._seen


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------

class TestMattermostRequirements:
    def test_check_requirements_with_token_and_url(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "test-token")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")
        from plugins.platforms.mattermost.adapter import check_mattermost_requirements
        assert check_mattermost_requirements() is True

    def test_check_requirements_without_token(self, monkeypatch):
        monkeypatch.delenv("MATTERMOST_TOKEN", raising=False)
        monkeypatch.delenv("MATTERMOST_URL", raising=False)
        from plugins.platforms.mattermost.adapter import check_mattermost_requirements
        assert check_mattermost_requirements() is False

    def test_check_requirements_without_url(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "test-token")
        monkeypatch.delenv("MATTERMOST_URL", raising=False)
        from plugins.platforms.mattermost.adapter import check_mattermost_requirements
        assert check_mattermost_requirements() is False


# ---------------------------------------------------------------------------
# Media type propagation (MIME types, not bare strings)
# ---------------------------------------------------------------------------

class TestMattermostMediaTypes:
    """Verify that media_types contains actual MIME types (e.g. 'image/png')
    rather than bare category strings ('image'), so downstream
    ``mtype.startswith("image/")`` checks in run.py work correctly."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter.handle_message = AsyncMock()

    def _make_event(self, file_ids):
        post_data = {
            "id": "post_media",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id file attached",
            "file_ids": file_ids,
        }
        return {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

    @pytest.mark.asyncio
    async def test_image_media_type_is_full_mime(self):
        """An image attachment should produce 'image/png', not 'image'."""
        file_info = {"name": "photo.png", "mime_type": "image/png"}
        self.adapter._api_get = AsyncMock(return_value=file_info)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"\x89PNG fake")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        self.adapter._session = MagicMock()
        self.adapter._session.get = MagicMock(return_value=mock_resp)

        with patch("gateway.platforms.base.cache_image_from_bytes", return_value="/tmp/photo.png"):
            await self.adapter._handle_ws_event(self._make_event(["file1"]))

        msg = self.adapter.handle_message.call_args[0][0]
        assert msg.media_types == ["image/png"]
        assert msg.media_types[0].startswith("image/")

    @pytest.mark.asyncio
    async def test_audio_media_type_is_full_mime(self):
        """An audio attachment should produce 'audio/ogg', not 'audio'."""
        file_info = {"name": "voice.ogg", "mime_type": "audio/ogg"}
        self.adapter._api_get = AsyncMock(return_value=file_info)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"OGG fake")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        self.adapter._session = MagicMock()
        self.adapter._session.get = MagicMock(return_value=mock_resp)

        with patch("gateway.platforms.base.cache_audio_from_bytes", return_value="/tmp/voice.ogg"), \
             patch("gateway.platforms.base.cache_image_from_bytes"), \
             patch("gateway.platforms.base.cache_document_from_bytes"):
            await self.adapter._handle_ws_event(self._make_event(["file2"]))

        msg = self.adapter.handle_message.call_args[0][0]
        assert msg.media_types == ["audio/ogg"]
        assert msg.media_types[0].startswith("audio/")

    @pytest.mark.asyncio
    async def test_document_media_type_is_full_mime(self):
        """A document attachment should produce 'application/pdf', not 'document'."""
        file_info = {"name": "report.pdf", "mime_type": "application/pdf"}
        self.adapter._api_get = AsyncMock(return_value=file_info)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"PDF fake")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        self.adapter._session = MagicMock()
        self.adapter._session.get = MagicMock(return_value=mock_resp)

        with patch("gateway.platforms.base.cache_document_from_bytes", return_value="/tmp/report.pdf"), \
             patch("gateway.platforms.base.cache_image_from_bytes"):
            await self.adapter._handle_ws_event(self._make_event(["file3"]))

        msg = self.adapter.handle_message.call_args[0][0]
        assert msg.media_types == ["application/pdf"]
        assert not msg.media_types[0].startswith("image/")
        assert not msg.media_types[0].startswith("audio/")



@pytest.mark.asyncio
async def test_mattermost_top_level_channel_post_is_thread_root():
    adapter = _make_adapter()
    adapter._reply_mode = "thread"
    adapter._bot_user_id = "bot_user_id"
    adapter._bot_username = "hermes-bot"
    adapter.handle_message = AsyncMock()
    post_data = {
        "id": "top_post_123",
        "user_id": "user_123",
        "channel_id": "chan_456",
        "message": "@hermes-bot start work",
        "root_id": "",
    }
    event = {
        "event": "posted",
        "data": {
            "post": json.dumps(post_data),
            "channel_type": "O",
            "sender_name": "@alice",
        },
    }

    await adapter._handle_ws_event(event)

    msg_event = adapter.handle_message.call_args[0][0]
    assert msg_event.source.thread_id == "top_post_123"
    assert msg_event.source.message_id == "top_post_123"
    assert msg_event.message_id == "top_post_123"


@pytest.mark.asyncio
async def test_mattermost_dm_post_does_not_seed_thread_root():
    adapter = _make_adapter()
    adapter._reply_mode = "thread"
    adapter._bot_user_id = "bot_user_id"
    adapter._bot_username = "hermes-bot"
    adapter.handle_message = AsyncMock()
    post_data = {
        "id": "dm_post_123",
        "user_id": "user_123",
        "channel_id": "dm_chan",
        "message": "hello",
        "root_id": "",
    }
    event = {
        "event": "posted",
        "data": {
            "post": json.dumps(post_data),
            "channel_type": "D",
            "sender_name": "@alice",
        },
    }

    await adapter._handle_ws_event(event)

    msg_event = adapter.handle_message.call_args[0][0]
    assert msg_event.source.thread_id is None
    assert msg_event.source.message_id == "dm_post_123"
