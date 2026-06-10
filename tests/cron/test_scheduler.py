"""Tests for cron/scheduler.py — origin resolution, delivery routing, and error logging."""

import json
import logging
import os
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from cron.scheduler import _resolve_origin, _resolve_delivery_target, _deliver_result, _send_media_via_adapter, run_job, SILENT_MARKER, _build_job_prompt, _strip_iteration_markers
from tools.env_passthrough import clear_env_passthrough
from tools.credential_files import clear_credential_files


@pytest.fixture
def _tick_lock_isolated(tmp_path):
    """Redirect scheduler tick lock to a per-test temp dir.

    tick() acquires an exclusive file lock at the path returned by
    _get_lock_paths() (derived at call time from _get_hermes_home()); under
    pytest-xdist parallel workers this races and lock-losers short-circuit
    with `return 0` before _process_job runs, breaking any test that asserts
    positive behavior (delivery called, output saved, log emitted).

    Test classes that call tick() must opt in via
    @pytest.mark.usefixtures("_tick_lock_isolated").
    """
    # v0.15.1 catch-up: upstream replaced the module-level _LOCK_DIR/_LOCK_FILE
    # constants with _get_lock_paths(), which derives the lock dir from the
    # _hermes_home override hook. Patch that hook so the lock lands under tmp.
    with patch("cron.scheduler._hermes_home", tmp_path):
        yield


class TestResolveOrigin:
    def test_full_origin(self):
        job = {
            "origin": {
                "platform": "telegram",
                "chat_id": "123456",
                "chat_name": "Test Chat",
                "thread_id": "42",
            }
        }
        result = _resolve_origin(job)
        assert isinstance(result, dict)
        assert result == job["origin"]
        assert result["platform"] == "telegram"
        assert result["chat_id"] == "123456"
        assert result["chat_name"] == "Test Chat"
        assert result["thread_id"] == "42"

    def test_no_origin(self):
        assert _resolve_origin({}) is None
        assert _resolve_origin({"origin": None}) is None

    def test_missing_platform(self):
        job = {"origin": {"chat_id": "123"}}
        assert _resolve_origin(job) is None

    def test_missing_chat_id(self):
        job = {"origin": {"platform": "telegram"}}
        assert _resolve_origin(job) is None

    def test_empty_origin(self):
        job = {"origin": {}}
        assert _resolve_origin(job) is None

    def test_string_origin_returns_none(self):
        # Regression: jobs.json entries created by the 2026-04-26 matcher-shadow
        # recovery plan stored a provenance tag in the `origin` field as a bare
        # string. The scheduler treated the field as a dict and crashed every
        # tick with `'str' object has no attribute 'get'`. Non-dict origins
        # must be coerced to None like any other malformed routing metadata.
        job = {"origin": "matcher-shadow-coverage-recovery-2026-04-26"}
        assert _resolve_origin(job) is None

    @pytest.mark.parametrize(
        "non_dict_origin",
        [
            "combined-digest-replaces-x-and-y-20260503",
            123,
            ["telegram", "12345"],
            ("platform", "chat_id"),
            42.0,
        ],
    )
    def test_non_dict_origin_returns_none_instead_of_crashing(self, non_dict_origin):
        """Non-dict origins (provenance strings from hand-edited or migrated
        jobs.json) must be treated as missing instead of crashing the
        scheduler tick on ``origin.get('platform')`` with
        ``'str' object has no attribute 'get'`` (#18722).

        Before this guard a job in this state crashed every fire attempt
        forever; ``mark_job_run`` recorded the error but the next tick
        re-loaded the poisoned origin and crashed identically.
        """
        job = {"origin": non_dict_origin}
        assert _resolve_origin(job) is None


class TestResolveDeliveryTarget:
    def test_origin_delivery_preserves_thread_id(self):
        job = {
            "deliver": "origin",
            "origin": {
                "platform": "telegram",
                "chat_id": "-1001",
                "thread_id": "17585",
            },
        }

        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-1001",
            "thread_id": "17585",
        }

    @pytest.mark.parametrize(
        ("platform", "env_var", "chat_id"),
        [
            ("matrix", "MATRIX_HOME_ROOM", "!bot-room:example.org"),
            ("signal", "SIGNAL_HOME_CHANNEL", "+15551234567"),
            ("mattermost", "MATTERMOST_HOME_CHANNEL", "team-town-square"),
            ("sms", "SMS_HOME_CHANNEL", "+15557654321"),
            ("email", "EMAIL_HOME_ADDRESS", "home@example.com"),
            ("dingtalk", "DINGTALK_HOME_CHANNEL", "cidNNN"),
            ("feishu", "FEISHU_HOME_CHANNEL", "oc_home"),
            ("wecom", "WECOM_HOME_CHANNEL", "wecom-home"),
            ("weixin", "WEIXIN_HOME_CHANNEL", "wxid_home"),
            ("qqbot", "QQ_HOME_CHANNEL", "group-openid-home"),
        ],
    )
    def test_origin_delivery_without_origin_falls_back_to_supported_home_channels(
        self, monkeypatch, platform, env_var, chat_id
    ):
        for fallback_env in (
            "MATRIX_HOME_ROOM",
            "MATRIX_HOME_CHANNEL",
            "TELEGRAM_HOME_CHANNEL",
            "DISCORD_HOME_CHANNEL",
            "SLACK_HOME_CHANNEL",
            "SIGNAL_HOME_CHANNEL",
            "MATTERMOST_HOME_CHANNEL",
            "SMS_HOME_CHANNEL",
            "EMAIL_HOME_ADDRESS",
            "DINGTALK_HOME_CHANNEL",
            "BLUEBUBBLES_HOME_CHANNEL",
            "FEISHU_HOME_CHANNEL",
            "WECOM_HOME_CHANNEL",
            "WEIXIN_HOME_CHANNEL",
            "QQ_HOME_CHANNEL",
        ):
            monkeypatch.delenv(fallback_env, raising=False)
        monkeypatch.setenv(env_var, chat_id)

        assert _resolve_delivery_target({"deliver": "origin"}) == {
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": None,
        }

    def test_bare_matrix_delivery_uses_matrix_home_room(self, monkeypatch):
        monkeypatch.delenv("MATRIX_HOME_CHANNEL", raising=False)
        monkeypatch.setenv("MATRIX_HOME_ROOM", "!room123:example.org")

        assert _resolve_delivery_target({"deliver": "matrix"}) == {
            "platform": "matrix",
            "chat_id": "!room123:example.org",
            "thread_id": None,
        }

    def test_bare_whatsapp_delivery_uses_whatsapp_home_channel(self, monkeypatch):
        """Regression: _HOME_TARGET_ENV_VARS was missing a 'whatsapp' entry,
        so bare deliver=whatsapp jobs (cron jobs + whatsapp_escalator's
        synthetic event-bus job + digest_composer) all failed with
        'no delivery target resolved for deliver=whatsapp' even when
        WHATSAPP_HOME_CHANNEL was set."""
        monkeypatch.setenv("WHATSAPP_HOME_CHANNEL", "53910513393901@lid")

        assert _resolve_delivery_target({"deliver": "whatsapp"}) == {
            "platform": "whatsapp",
            "chat_id": "53910513393901@lid",
            "thread_id": None,
        }

    def test_bare_platform_delivery_preserves_home_thread_id(self, monkeypatch):
        monkeypatch.setenv("DISCORD_HOME_CHANNEL", "parent-42")
        monkeypatch.setenv("DISCORD_HOME_CHANNEL_THREAD_ID", "topic-7")

        assert _resolve_delivery_target({"deliver": "discord"}) == {
            "platform": "discord",
            "chat_id": "parent-42",
            "thread_id": "topic-7",
        }

    def test_telegram_cron_thread_id_overrides_home_thread_id(self, monkeypatch):
        """TELEGRAM_CRON_THREAD_ID wins over TELEGRAM_HOME_CHANNEL_THREAD_ID for cron (#24409)."""
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-1001234567890")
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "5")
        monkeypatch.setenv("TELEGRAM_CRON_THREAD_ID", "42")

        assert _resolve_delivery_target({"deliver": "telegram"}) == {
            "platform": "telegram",
            "chat_id": "-1001234567890",
            "thread_id": "42",
        }

    def test_telegram_cron_thread_id_sets_thread_when_home_thread_unset(self, monkeypatch):
        """TELEGRAM_CRON_THREAD_ID supplies a thread when no home thread is configured."""
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-1001234567890")
        monkeypatch.delenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", raising=False)
        monkeypatch.setenv("TELEGRAM_CRON_THREAD_ID", "42")

        assert _resolve_delivery_target({"deliver": "telegram"}) == {
            "platform": "telegram",
            "chat_id": "-1001234567890",
            "thread_id": "42",
        }

    def test_telegram_cron_thread_id_does_not_leak_to_other_platforms(self, monkeypatch):
        """TELEGRAM_CRON_THREAD_ID is Telegram-only; other platforms keep their own thread resolution."""
        monkeypatch.setenv("DISCORD_HOME_CHANNEL", "parent-42")
        monkeypatch.setenv("DISCORD_HOME_CHANNEL_THREAD_ID", "topic-7")
        monkeypatch.setenv("TELEGRAM_CRON_THREAD_ID", "42")

        assert _resolve_delivery_target({"deliver": "discord"}) == {
            "platform": "discord",
            "chat_id": "parent-42",
            "thread_id": "topic-7",
        }

    def test_explicit_telegram_topic_target_overrides_cron_thread_id(self, monkeypatch):
        """Explicit ``telegram:chat:thread`` targets bypass TELEGRAM_CRON_THREAD_ID."""
        monkeypatch.setenv("TELEGRAM_CRON_THREAD_ID", "999")

        job = {"deliver": "telegram:-1003724596514:17"}
        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-1003724596514",
            "thread_id": "17",
        }

    def test_explicit_telegram_topic_target_with_thread_id(self):
        """deliver: 'telegram:chat_id:thread_id' parses correctly."""
        job = {
            "deliver": "telegram:-1003724596514:17",
        }
        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-1003724596514",
            "thread_id": "17",
        }

    def test_explicit_telegram_topic_thread_survives_bare_directory_match(self):
        """Exact channel-directory matches must not erase an explicit topic id."""
        job = {
            "deliver": "telegram:-1003724596514:17",
        }
        with patch(
            "gateway.channel_directory.resolve_channel_name",
            return_value="-1003724596514",
        ):
            result = _resolve_delivery_target(job)
        assert result == {
            "platform": "telegram",
            "chat_id": "-1003724596514",
            "thread_id": "17",
        }

    def test_explicit_telegram_chat_id_without_thread_id(self):
        """deliver: 'telegram:chat_id' sets thread_id to None."""
        job = {
            "deliver": "telegram:-1003724596514",
        }
        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-1003724596514",
            "thread_id": None,
        }

    def test_human_friendly_label_resolved_via_channel_directory(self):
        """deliver: 'whatsapp:Alice (dm)' resolves to the real JID."""
        job = {"deliver": "whatsapp:Alice (dm)"}
        with patch(
            "gateway.channel_directory.resolve_channel_name",
            return_value="12345678901234@lid",
        ) as resolve_mock:
            result = _resolve_delivery_target(job)
        resolve_mock.assert_called_once_with("whatsapp", "Alice (dm)")
        assert result == {
            "platform": "whatsapp",
            "chat_id": "12345678901234@lid",
            "thread_id": None,
        }

    def test_human_friendly_label_without_suffix_resolved(self):
        """deliver: 'telegram:My Group' resolves without display suffix."""
        job = {"deliver": "telegram:My Group"}
        with patch(
            "gateway.channel_directory.resolve_channel_name",
            return_value="-1009999",
        ):
            result = _resolve_delivery_target(job)
        assert result == {
            "platform": "telegram",
            "chat_id": "-1009999",
            "thread_id": None,
        }

    def test_human_friendly_topic_label_preserves_thread_id(self):
        """Resolved Telegram topic labels should split chat_id and thread_id."""
        job = {"deliver": "telegram:Coaching Chat / topic 17585 (group)"}
        with patch(
            "gateway.channel_directory.resolve_channel_name",
            return_value="-1009999:17585",
        ):
            result = _resolve_delivery_target(job)
        assert result == {
            "platform": "telegram",
            "chat_id": "-1009999",
            "thread_id": "17585",
        }

    def test_raw_id_not_mangled_when_directory_returns_none(self):
        """deliver: 'whatsapp:12345@lid' passes through when directory has no match."""
        job = {"deliver": "whatsapp:12345@lid"}
        with patch(
            "gateway.channel_directory.resolve_channel_name",
            return_value=None,
        ):
            result = _resolve_delivery_target(job)
        assert result == {
            "platform": "whatsapp",
            "chat_id": "12345@lid",
            "thread_id": None,
        }

    def test_bare_platform_uses_matching_origin_chat(self):
        job = {
            "deliver": "telegram",
            "origin": {
                "platform": "telegram",
                "chat_id": "-1001",
                "thread_id": "17585",
            },
        }

        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-1001",
            "thread_id": "17585",
        }

    def test_bare_platform_falls_back_to_home_channel(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-2002")
        job = {
            "deliver": "telegram",
            "origin": {
                "platform": "discord",
                "chat_id": "abc",
            },
        }

        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-2002",
            "thread_id": None,
        }

    def test_explicit_discord_topic_target_with_thread_id(self):
        """deliver: 'discord:chat_id:thread_id' parses correctly."""
        job = {
            "deliver": "discord:-1001234567890:17585",
        }
        assert _resolve_delivery_target(job) == {
            "platform": "discord",
            "chat_id": "-1001234567890",
            "thread_id": "17585",
        }

    def test_explicit_discord_chat_id_without_thread_id(self):
        """deliver: 'discord:chat_id' sets thread_id to None."""
        job = {
            "deliver": "discord:9876543210",
        }
        assert _resolve_delivery_target(job) == {
            "platform": "discord",
            "chat_id": "9876543210",
            "thread_id": None,
        }

    def test_explicit_discord_channel_without_thread(self):
        """deliver: 'discord:1001234567890' resolves via explicit platform:chat_id path."""
        job = {
            "deliver": "discord:1001234567890",
        }
        result = _resolve_delivery_target(job)
        assert result == {
            "platform": "discord",
            "chat_id": "1001234567890",
            "thread_id": None,
        }

    def test_list_form_deliver_is_normalized(self, monkeypatch):
        """deliver=['telegram'] (Python list) should resolve like 'telegram' string.

        Regression test for #17139: MCP clients / scripts that pass the deliver
        field as an array-shaped value used to fail with "no delivery target
        resolved for deliver=['telegram']" because ``str(['telegram'])`` was
        passed through to ``split(',')`` verbatim.
        """
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-4004")
        job = {
            "deliver": ["telegram"],
            "origin": None,
        }

        assert _resolve_delivery_target(job) == {
            "platform": "telegram",
            "chat_id": "-4004",
            "thread_id": None,
        }

    def test_list_form_multiple_platforms_normalized(self, monkeypatch):
        """deliver=['telegram', 'discord'] resolves to multiple targets."""
        from cron.scheduler import _resolve_delivery_targets

        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-111")
        monkeypatch.setenv("DISCORD_HOME_CHANNEL", "-222")
        job = {"deliver": ["telegram", "discord"], "origin": None}

        targets = _resolve_delivery_targets(job)
        platforms = sorted(t["platform"] for t in targets)
        assert platforms == ["discord", "telegram"]

    def test_empty_list_form_deliver_resolves_to_local(self):
        """deliver=[] is treated as local (no delivery)."""
        from cron.scheduler import _resolve_delivery_targets

        assert _resolve_delivery_targets({"deliver": []}) == []


class TestRoutingIntents:
    """``all`` routing intent expands at fire time."""

    def test_all_expands_to_every_connected_home_channel(self, monkeypatch):
        """deliver='all' fans out to every platform with a configured home channel."""
        from cron.scheduler import _resolve_delivery_targets

        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-111")
        monkeypatch.setenv("DISCORD_HOME_CHANNEL", "-222")
        monkeypatch.setenv("SLACK_HOME_CHANNEL", "C333")
        # Sanity: platforms without the env var must NOT appear in the expansion.
        monkeypatch.delenv("SIGNAL_HOME_CHANNEL", raising=False)
        monkeypatch.delenv("MATRIX_HOME_ROOM", raising=False)

        targets = _resolve_delivery_targets({"deliver": "all", "origin": None})
        platforms = sorted(t["platform"] for t in targets)

        assert "telegram" in platforms
        assert "discord" in platforms
        assert "slack" in platforms
        assert "signal" not in platforms
        assert "matrix" not in platforms

    def test_all_combines_with_explicit_target_and_dedups(self, monkeypatch):
        """'telegram:-999,all' yields every home channel + the explicit target without dupes."""
        from cron.scheduler import _resolve_delivery_targets

        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-111")
        monkeypatch.setenv("DISCORD_HOME_CHANNEL", "-222")

        # Explicit telegram target precedes 'all'. Expansion adds discord;
        # the dedup pass collapses any (platform, chat_id, thread_id) repeats.
        job = {"deliver": "telegram:-999,all", "origin": None}
        targets = _resolve_delivery_targets(job)

        platforms = sorted(t["platform"].lower() for t in targets)
        assert "telegram" in platforms
        assert "discord" in platforms
        # Every target is unique on (platform, chat_id, thread_id).
        keys = [(t["platform"].lower(), str(t["chat_id"]), t.get("thread_id")) for t in targets]
        assert len(keys) == len(set(keys))

    def test_all_with_no_connected_channels_returns_empty(self, monkeypatch):
        """deliver='all' with nothing connected returns [] — delivery is recorded as failed upstream."""
        from cron.scheduler import _resolve_delivery_targets

        for var in ("TELEGRAM_HOME_CHANNEL", "DISCORD_HOME_CHANNEL", "SLACK_HOME_CHANNEL",
                    "SIGNAL_HOME_CHANNEL", "MATRIX_HOME_ROOM", "MATTERMOST_HOME_CHANNEL",
                    "SMS_HOME_CHANNEL", "EMAIL_HOME_ADDRESS", "DINGTALK_HOME_CHANNEL",
                    "FEISHU_HOME_CHANNEL", "WECOM_HOME_CHANNEL", "WEIXIN_HOME_CHANNEL",
                    "BLUEBUBBLES_HOME_CHANNEL", "QQBOT_HOME_CHANNEL", "QQ_HOME_CHANNEL"):
            monkeypatch.delenv(var, raising=False)

        assert _resolve_delivery_targets({"deliver": "all", "origin": None}) == []

    def test_origin_comma_all_preserves_origin_first(self, monkeypatch):
        """'origin,all' delivers to the origin platform plus every other home channel."""
        from cron.scheduler import _resolve_delivery_targets

        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-111")
        monkeypatch.setenv("DISCORD_HOME_CHANNEL", "-222")

        job = {
            "deliver": "origin,all",
            "origin": {"platform": "discord", "chat_id": "888"},
        }
        targets = _resolve_delivery_targets(job)
        platforms = sorted(t["platform"].lower() for t in targets)
        assert "telegram" in platforms
        assert "discord" in platforms

        # The origin's explicit chat_id (888) wins the dedup race over the
        # discord home channel (-222) because origin is resolved first.
        discord = next(t for t in targets if t["platform"].lower() == "discord")
        assert discord["chat_id"] == "888"

    def test_all_token_case_insensitive(self, monkeypatch):
        """'ALL' / 'All' / 'all' are all recognized."""
        from cron.scheduler import _resolve_delivery_targets

        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-111")
        monkeypatch.setenv("DISCORD_HOME_CHANNEL", "-222")

        for token in ("ALL", "All", "all"):
            targets = _resolve_delivery_targets({"deliver": token, "origin": None})
            platforms = sorted(t["platform"].lower() for t in targets)
            assert platforms == ["discord", "telegram"], f"token={token!r} -> {platforms}"


class TestDeliverResultWrapping:
    """Verify that cron deliveries are wrapped with header/footer and no longer mirrored."""

    def _safe_media_path(self, tmp_path, monkeypatch, name, data=b"media"):
        root = tmp_path / "media-cache"
        media_file = root / name
        media_file.parent.mkdir(parents=True, exist_ok=True)
        media_file.write_bytes(data)
        monkeypatch.setattr(
            "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
            (root,),
        )
        return media_file.resolve()

    def test_delivery_wraps_content_with_header_and_footer(self):
        """Delivered content should include task name header and agent-invisible note."""
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock:
            job = {
                "id": "test-job",
                "name": "daily-report",
                "deliver": "origin",
                "origin": {"platform": "telegram", "chat_id": "123"},
            }
            _deliver_result(job, "Here is today's summary.")

        send_mock.assert_called_once()
        sent_content = send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][-1]
        assert "Cronjob Response: daily-report" in sent_content
        assert "(job_id: test-job)" in sent_content
        assert "-------------" in sent_content
        assert "Here is today's summary." in sent_content
        assert "To stop or manage this job" in sent_content

    def test_delivery_uses_job_id_when_no_name(self):
        """When a job has no name, the wrapper should fall back to job id."""
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock:
            job = {
                "id": "abc-123",
                "deliver": "origin",
                "origin": {"platform": "telegram", "chat_id": "123"},
            }
            _deliver_result(job, "Output.")

        sent_content = send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][-1]
        assert "Cronjob Response: abc-123" in sent_content

    def test_delivery_skips_wrapping_when_config_disabled(self):
        """When cron.wrap_response is false, deliver raw content without header/footer."""
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}):
            job = {
                "id": "test-job",
                "name": "daily-report",
                "deliver": "origin",
                "origin": {"platform": "telegram", "chat_id": "123"},
            }
            _deliver_result(job, "Clean output only.")

        send_mock.assert_called_once()
        sent_content = send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][-1]
        assert sent_content == "Clean output only."
        assert "Cronjob Response" not in sent_content
        assert "The agent cannot see" not in sent_content

    def test_delivery_strips_agent_iteration_marker(self):
        """The internal <AGENT_ITERATION_JSON> contract block must never reach
        the delivered body. It is machine-only (consumed by the cron event-bus
        extractors); leaking it makes Telegram's HTML parser reject the
        unsupported <agent_iteration_json> tag and fall back to plain text on
        every send (issue: 'Parse mode HTML failed in _send_telegram')."""
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        final_response = (
            "Scanned 4 sources, 23 new jobs (11 deduped).\n\n"
            "<AGENT_ITERATION_JSON>\n"
            '{"agent": "scout", "summary": "Scanned 4 sources, 23 new jobs"}\n'
            "</AGENT_ITERATION_JSON>"
        )

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}):
            job = {
                "id": "scout-job",
                "name": "scout-scan",
                "deliver": "origin",
                "origin": {"platform": "telegram", "chat_id": "123"},
            }
            _deliver_result(job, final_response)

        send_mock.assert_called_once()
        sent_content = send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][-1]
        # Marker tag and its JSON payload are gone; user-facing preamble stays.
        assert "AGENT_ITERATION_JSON" not in sent_content
        assert '"agent": "scout"' not in sent_content
        assert sent_content == "Scanned 4 sources, 23 new jobs (11 deduped)."

    def test_delivery_strips_legacy_tailor_iteration_marker(self):
        """The legacy Tailor-specific <TAILOR_ITERATION_JSON> block leaks the
        same way and must also be stripped before delivery."""
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        final_response = (
            "Tailored 3 of 5 eligible packets.\n\n"
            "<TAILOR_ITERATION_JSON>\n"
            '{"eligible_count": 5, "tailored_count": 3, "skipped_terminal_count": 2, '
            '"skipped_other_count": 0, "reason": "tailored_some"}\n'
            "</TAILOR_ITERATION_JSON>"
        )

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}):
            job = {
                "id": "tailor-job",
                "name": "jobflow-tailor",
                "deliver": "origin",
                "origin": {"platform": "telegram", "chat_id": "123"},
            }
            _deliver_result(job, final_response)

        send_mock.assert_called_once()
        sent_content = send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][-1]
        assert "TAILOR_ITERATION_JSON" not in sent_content
        assert "eligible_count" not in sent_content
        assert sent_content == "Tailored 3 of 5 eligible packets."

    def test_delivery_extracts_media_tags_before_send(self, tmp_path, monkeypatch):
        """Cron delivery should pass MEDIA attachments separately to the send helper."""
        from gateway.config import Platform
        media_path = self._safe_media_path(tmp_path, monkeypatch, "test-voice.ogg")

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}):
            job = {
                "id": "voice-job",
                "deliver": "origin",
                "origin": {"platform": "telegram", "chat_id": "123"},
            }
            _deliver_result(job, f"Title\nMEDIA:{media_path}")

        send_mock.assert_called_once()
        args, kwargs = send_mock.call_args
        # Text content should have MEDIA: tag stripped
        assert "MEDIA:" not in args[3]
        assert "Title" in args[3]
        # Media files should be forwarded separately
        assert kwargs["media_files"] == [(str(media_path), False)]

    def test_live_adapter_sends_media_as_attachments(self, tmp_path, monkeypatch):
        """When a live adapter is available, MEDIA files should be sent as native
        platform attachments (e.g., Discord voice, Telegram audio) rather than
        as literal 'MEDIA:/path' text."""
        from gateway.config import Platform
        from concurrent.futures import Future
        media_path = self._safe_media_path(tmp_path, monkeypatch, "cron-voice.mp3")

        adapter = AsyncMock()
        adapter.send.return_value = MagicMock(success=True)
        adapter.send_voice.return_value = MagicMock(success=True)

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.DISCORD: pconfig}

        loop = MagicMock()
        loop.is_running.return_value = True

        # run_coroutine_threadsafe returns concurrent.futures.Future (has timeout kwarg)
        def fake_run_coro(coro, _loop):
            future = Future()
            future.set_result(MagicMock(success=True))
            coro.close()
            return future

        job = {
            "id": "tts-job",
            "deliver": "origin",
            "origin": {"platform": "discord", "chat_id": "9876"},
        }

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
            _deliver_result(
                job,
                f"Here is TTS\nMEDIA:{media_path}",
                adapters={Platform.DISCORD: adapter},
                loop=loop,
            )

        # Text should be sent without the MEDIA tag
        adapter.send.assert_called_once()
        text_sent = adapter.send.call_args[0][1]
        assert "MEDIA:" not in text_sent
        assert "Here is TTS" in text_sent

        # Audio file should be sent as a voice attachment
        adapter.send_voice.assert_called_once()
        voice_call = adapter.send_voice.call_args
        assert voice_call[1]["audio_path"] == str(media_path)

    def test_live_adapter_routes_image_to_send_image_file(self, tmp_path, monkeypatch):
        """Image MEDIA files should be routed to send_image_file, not send_voice."""
        from gateway.config import Platform
        from concurrent.futures import Future
        media_path = self._safe_media_path(tmp_path, monkeypatch, "chart.png")

        adapter = AsyncMock()
        adapter.send.return_value = MagicMock(success=True)
        adapter.send_image_file.return_value = MagicMock(success=True)

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.DISCORD: pconfig}

        loop = MagicMock()
        loop.is_running.return_value = True

        def fake_run_coro(coro, _loop):
            future = Future()
            future.set_result(MagicMock(success=True))
            coro.close()
            return future

        job = {
            "id": "img-job",
            "deliver": "origin",
            "origin": {"platform": "discord", "chat_id": "1234"},
        }

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
            _deliver_result(
                job,
                f"Chart attached\nMEDIA:{media_path}",
                adapters={Platform.DISCORD: adapter},
                loop=loop,
            )

        adapter.send_image_file.assert_called_once()
        assert adapter.send_image_file.call_args[1]["image_path"] == str(media_path)
        adapter.send_voice.assert_not_called()

    def test_live_adapter_media_only_no_text(self, tmp_path, monkeypatch):
        """When content is ONLY a MEDIA tag with no text, media should still be sent."""
        from gateway.config import Platform
        from concurrent.futures import Future
        media_path = self._safe_media_path(tmp_path, monkeypatch, "voice.ogg")

        adapter = AsyncMock()
        adapter.send_voice.return_value = MagicMock(success=True)

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        loop = MagicMock()
        loop.is_running.return_value = True

        def fake_run_coro(coro, _loop):
            future = Future()
            future.set_result(MagicMock(success=True))
            coro.close()
            return future

        job = {
            "id": "voice-only",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "999"},
        }

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
            _deliver_result(
                job,
                f"[[audio_as_voice]]\nMEDIA:{media_path}",
                adapters={Platform.TELEGRAM: adapter},
                loop=loop,
            )

        # Text send should NOT be called (no text after stripping MEDIA tag)
        adapter.send.assert_not_called()
        # Audio should still be delivered as a voice bubble
        adapter.send_voice.assert_called_once()

    def test_live_adapter_sends_cleaned_text_not_raw(self):
        """The live adapter path must send cleaned text (MEDIA tags stripped),
        not the raw delivery_content with embedded MEDIA: tags."""
        from gateway.config import Platform
        from concurrent.futures import Future

        adapter = AsyncMock()
        adapter.send.return_value = MagicMock(success=True)

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        loop = MagicMock()
        loop.is_running.return_value = True

        def fake_run_coro(coro, _loop):
            future = Future()
            future.set_result(MagicMock(success=True))
            coro.close()
            return future

        job = {
            "id": "img-job",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "555"},
        }

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
            _deliver_result(
                job,
                "Report\nMEDIA:/tmp/chart.png",
                adapters={Platform.TELEGRAM: adapter},
                loop=loop,
            )

        text_sent = adapter.send.call_args[0][1]
        assert "MEDIA:" not in text_sent
        assert "Report" in text_sent

    def test_no_mirror_to_session_call(self):
        """Cron deliveries should NOT mirror into the gateway session."""
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})), \
             patch("gateway.mirror.mirror_to_session") as mirror_mock:
            job = {
                "id": "test-job",
                "deliver": "origin",
                "origin": {"platform": "telegram", "chat_id": "123"},
            }
            _deliver_result(job, "Hello!")

        mirror_mock.assert_not_called()

    def test_origin_delivery_preserves_thread_id(self):
        """Origin delivery should forward thread_id to the send helper."""
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        job = {
            "id": "test-job",
            "name": "topic-job",
            "deliver": "origin",
            "origin": {
                "platform": "telegram",
                "chat_id": "-1001",
                "thread_id": "17585",
            },
        }

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock:
            _deliver_result(job, "hello")

        send_mock.assert_called_once()
        assert send_mock.call_args.kwargs["thread_id"] == "17585"


class TestDeliverResultErrorReturns:
    """Verify _deliver_result returns error strings on failure, None on success."""

    def test_returns_error_when_platform_disabled(self):
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = False
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg):
            job = {
                "id": "disabled",
                "deliver": "origin",
                "origin": {"platform": "telegram", "chat_id": "123"},
            }
            result = _deliver_result(job, "Output.")
        assert result is not None
        assert "not configured" in result

    def test_returns_error_for_unresolved_target(self, monkeypatch):
        """Non-local delivery with no resolvable target should return an error."""
        monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
        job = {"id": "no-target", "deliver": "telegram"}
        result = _deliver_result(job, "Output.")
        assert result is not None
        assert "no delivery target" in result


@pytest.mark.usefixtures("_tick_lock_isolated")
class TestRunJobSessionPersistence:
    def test_run_job_passes_session_db_and_cron_platform(self, tmp_path):
        job = {
            "id": "test-job",
            "name": "test",
            "prompt": "hello",
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "test-key",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        assert error is None
        assert final_response == "ok"
        assert "ok" in output

        kwargs = mock_agent_cls.call_args.kwargs
        assert kwargs["session_db"] is fake_db
        assert kwargs["platform"] == "cron"
        assert kwargs["session_id"].startswith("cron_test-job_")
        fake_db.end_session.assert_called_once()
        call_args = fake_db.end_session.call_args
        assert call_args[0][0].startswith("cron_test-job_")
        assert call_args[0][1] == "cron_complete"
        fake_db.close.assert_called_once()
        mock_agent.close.assert_called_once()

    def test_run_job_closes_agent_on_failure_to_prevent_fd_leak(self, tmp_path):
        # Regression: if ``run_conversation`` raises, the ephemeral cron
        # agent was previously leaked — over days of ticks this accumulated
        # httpx transports and hit EMFILE / "too many open files".
        job = {
            "id": "failing-job",
            "name": "failing",
            "prompt": "hello",
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.side_effect = RuntimeError("boom")
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is False
        assert final_response == ""
        assert "RuntimeError: boom" in error
        mock_agent.close.assert_called_once()

    def test_run_job_reaps_stale_auxiliary_clients_per_tick(self, tmp_path):
        # Regression: auxiliary clients bound to the cron worker's dead
        # event loop must be reaped each tick. Without this, ``_client_cache``
        # holds onto transports whose underlying sockets can no longer be
        # closed (their loop is gone), leaking one fd batch per cron run.
        job = {
            "id": "aux-clean-job",
            "name": "aux-clean",
            "prompt": "hello",
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls, \
             patch("agent.auxiliary_client.cleanup_stale_async_clients") as cleanup_mock:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, _output, _final_response, _error = run_job(job)

        assert success is True
        cleanup_mock.assert_called_once()

    def _make_run_job_patches(self, tmp_path):
        """Common patches for run_job tests."""
        fake_db = MagicMock()
        return fake_db, [
            patch("cron.scheduler._hermes_home", tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=fake_db),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
            ),
        ]

    def test_run_job_passes_enabled_toolsets_to_agent(self, tmp_path):
        job = {
            "id": "toolset-job",
            "name": "test",
            "prompt": "hello",
            "enabled_toolsets": ["web", "terminal", "file"],
        }
        fake_db, patches = self._make_run_job_patches(tmp_path)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            run_job(job)

        kwargs = mock_agent_cls.call_args.kwargs
        assert kwargs["enabled_toolsets"] == ["web", "terminal", "file"]

    def test_run_job_disabled_toolsets_layer_user_config_on_baseline(self, tmp_path):
        """agent.disabled_toolsets must be honoured in cron — issue #25752.

        The bug: per-job enabled_toolsets was returned verbatim, letting an
        LLM-supplied cronjob() call re-enable tools the operator had globally
        disabled. The fix: ALWAYS include agent.disabled_toolsets in the
        disabled_toolsets passed to AIAgent, on top of the cron baseline
        (cronjob/messaging/clarify). AIAgent's disabled_toolsets takes
        precedence over enabled_toolsets, so this stops the bypass.
        """
        (tmp_path / "config.yaml").write_text(
            "agent:\n"
            "  disabled_toolsets:\n"
            "    - terminal\n"
            "    - file\n",
            encoding="utf-8",
        )
        job = {
            "id": "policy-job",
            "name": "test",
            "prompt": "hello",
            "enabled_toolsets": ["web", "terminal", "file"],
        }
        fake_db, patches = self._make_run_job_patches(tmp_path)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            run_job(job)

        kwargs = mock_agent_cls.call_args.kwargs
        assert set(kwargs["disabled_toolsets"]) >= {
            "cronjob", "messaging", "clarify", "terminal", "file",
        }

    def test_run_job_enabled_toolsets_resolves_from_platform_config_when_not_set(self, tmp_path):
        """When a job has no explicit enabled_toolsets, the scheduler now
        resolves them from ``hermes tools`` platform config for ``cron``
        (PR #14xxx — blanket fix for Norbert's surprise ``moa`` run).

        The legacy "pass None → AIAgent loads full default" path is still
        reachable, but only when ``_get_platform_tools`` raises (safety net
        for any unexpected config shape).
        """
        job = {
            "id": "no-toolset-job",
            "name": "test",
            "prompt": "hello",
        }
        fake_db, patches = self._make_run_job_patches(tmp_path)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            run_job(job)

        kwargs = mock_agent_cls.call_args.kwargs
        # Resolution happened — not None, is a list.
        assert isinstance(kwargs["enabled_toolsets"], list)
        # The cron default is _HERMES_CORE_TOOLS with _DEFAULT_OFF_TOOLSETS
        # (``moa``, ``homeassistant``, ``rl``) removed. The most important
        # invariant: ``moa`` is NOT in the default cron toolset, so a cron
        # run cannot accidentally spin up frontier models.
        assert "moa" not in kwargs["enabled_toolsets"]

    def test_run_job_per_job_toolsets_win_over_platform_config(self, tmp_path):
        """Per-job enabled_toolsets (via cronjob tool) always take precedence
        over the platform-level ``hermes tools`` config."""
        job = {
            "id": "override-job",
            "name": "test",
            "prompt": "hello",
            "enabled_toolsets": ["terminal"],
        }
        fake_db, patches = self._make_run_job_patches(tmp_path)
        # Even if the user has ``hermes tools`` configured to enable web+file
        # for cron, the per-job override wins.
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch("run_agent.AIAgent") as mock_agent_cls, \
             patch(
                 "hermes_cli.tools_config._get_platform_tools",
                 return_value={"web", "file"},
             ):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            run_job(job)

        kwargs = mock_agent_cls.call_args.kwargs
        assert kwargs["enabled_toolsets"] == ["terminal"]

    def test_run_job_empty_response_returns_empty_not_placeholder(self, tmp_path):
        """Empty final_response should stay empty for delivery logic (issue #2234).

        The placeholder '(No response generated)' should only appear in the
        output log, not in the returned final_response that's used for delivery.
        """
        job = {
            "id": "silent-job",
            "name": "silent test",
            "prompt": "do work via tools only",
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            # Agent did work via tools but returned no text
            mock_agent.run_conversation.return_value = {"final_response": ""}
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        assert error is None
        # final_response should be empty for delivery logic to skip
        assert final_response == ""
        # But the output log should show the placeholder
        assert "(No response generated)" in output

    @pytest.mark.parametrize(
        "agent_result,expected_err_substring",
        [
            (
                {
                    "final_response": "API call failed after 3 retries: Request timed out.",
                    "failed": True,
                    "completed": False,
                    "error": "API call failed after 3 retries: Request timed out.",
                },
                "API call failed",
            ),
            (
                {"final_response": None, "completed": False, "failed": True},
                "agent reported failure",
            ),
            (
                {"final_response": "", "completed": False},
                "agent reported failure",
            ),
            (
                {
                    "final_response": "partial reply before crash",
                    "failed": True,
                    "completed": False,
                    "error": "model abort: connection reset",
                },
                "model abort",
            ),
        ],
    )
    def test_run_job_treats_agent_failure_flag_as_failure(
        self, tmp_path, agent_result, expected_err_substring
    ):
        """Issue #17855: run_conversation returns ``failed=True``/``completed=False``
        when the agent's API call exhausts retries or aborts mid-run. run_job
        must surface this as success=False so cron's last_status reflects the
        failure and the user gets an error notification, instead of treating
        the (often non-empty) error string in final_response as a legitimate
        agent reply.
        """
        job = {
            "id": "failing-api-job",
            "name": "failing api",
            "prompt": "do something",
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = agent_result
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is False
        assert final_response == ""
        assert error is not None and expected_err_substring in error
        # Output should be the FAILED template, not the success template.
        assert "(FAILED)" in output
        # Ephemeral cron agent must still be closed even on agent-flagged failure.
        mock_agent.close.assert_called_once()

    def test_run_job_completed_true_without_failed_flag_succeeds(self, tmp_path):
        """Regression guard: a normal success result (``completed=True``,
        ``failed`` absent) must not trip the failure-flag check.
        """
        job = {
            "id": "ok-job",
            "name": "ok",
            "prompt": "hello",
        }
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {
                "final_response": "all good",
                "completed": True,
            }
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        assert error is None
        assert final_response == "all good"

    def test_tick_marks_empty_response_as_error(self, tmp_path):
        """When run_job returns success=True but final_response is empty,
        tick() should mark the job as error so last_status != 'ok'.
        (issue #8585)
        """
        from cron.scheduler import tick
        from cron.jobs import load_jobs, save_jobs

        job = {
            "id": "empty-job",
            "name": "empty-test",
            "prompt": "do something",
            "schedule": "every 1h",
            "enabled": True,
            "next_run_at": "2020-01-01T00:00:00",
            "deliver": "local",
            "last_status": None,
        }

        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([job], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler.mark_job_run") as mock_mark, \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("cron.scheduler.run_job", return_value=(True, "output", "", None)):
            tick(verbose=False)

        # Should be called with success=False because final_response is empty
        mock_mark.assert_called_once()
        call_args = mock_mark.call_args
        assert call_args[0][0] == "empty-job"
        assert call_args[0][1] is False  # success should be False
        assert "empty" in call_args[0][2].lower()  # error should mention empty

    def test_run_job_sets_auto_delivery_env_from_dotenv_home_channel(self, tmp_path, monkeypatch):
        job = {
            "id": "test-job",
            "name": "test",
            "prompt": "hello",
            "deliver": "telegram",
        }
        fake_db = MagicMock()
        seen = {}

        (tmp_path / ".env").write_text("TELEGRAM_HOME_CHANNEL=-2002\n")
        monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
        monkeypatch.delenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", raising=False)
        monkeypatch.delenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", raising=False)
        monkeypatch.delenv("HERMES_CRON_AUTO_DELIVER_THREAD_ID", raising=False)

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

            def run_conversation(self, *args, **kwargs):
                from gateway.session_context import get_session_env
                seen["platform"] = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM") or None
                seen["chat_id"] = get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID") or None
                seen["thread_id"] = get_session_env("HERMES_CRON_AUTO_DELIVER_THREAD_ID") or None
                return {"final_response": "ok"}

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent", FakeAgent):
            success, output, final_response, error = run_job(job)

        assert success is True
        assert error is None
        assert final_response == "ok"
        assert "ok" in output
        assert seen == {
            "platform": "telegram",
            "chat_id": "-2002",
            "thread_id": None,
        }
        assert os.getenv("HERMES_CRON_AUTO_DELIVER_PLATFORM") is None
        assert os.getenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID") is None
        assert os.getenv("HERMES_CRON_AUTO_DELIVER_THREAD_ID") is None
        fake_db.close.assert_called_once()

    def test_run_job_clears_stale_auto_delivery_thread_id_between_jobs(self, tmp_path, monkeypatch):
        jobs = [
            {
                "id": "threaded-job",
                "name": "threaded",
                "prompt": "hello",
                "deliver": "telegram:-1001:42",
            },
            {
                "id": "threadless-job",
                "name": "threadless",
                "prompt": "hello again",
                "deliver": "telegram:-2002",
            },
        ]
        fake_db = MagicMock()
        seen = []

        monkeypatch.delenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", raising=False)
        monkeypatch.delenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", raising=False)
        monkeypatch.delenv("HERMES_CRON_AUTO_DELIVER_THREAD_ID", raising=False)

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

            def run_conversation(self, *args, **kwargs):
                from gateway.session_context import get_session_env

                seen.append(
                    {
                        "platform": get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM") or None,
                        "chat_id": get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID") or None,
                        "thread_id": get_session_env("HERMES_CRON_AUTO_DELIVER_THREAD_ID") or None,
                    }
                )
                return {"final_response": "ok"}

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent", FakeAgent):
            for job in jobs:
                success, output, final_response, error = run_job(job)
                assert success is True
                assert error is None
                assert final_response == "ok"
                assert "ok" in output

        assert seen == [
            {
                "platform": "telegram",
                "chat_id": "-1001",
                "thread_id": "42",
            },
            {
                "platform": "telegram",
                "chat_id": "-2002",
                "thread_id": None,
            },
        ]
        assert os.getenv("HERMES_CRON_AUTO_DELIVER_PLATFORM") is None
        assert os.getenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID") is None
        assert os.getenv("HERMES_CRON_AUTO_DELIVER_THREAD_ID") is None
        assert fake_db.close.call_count == 2


class TestRunJobConfigLogging:
    """Verify that config.yaml parse failures are logged, not silently swallowed."""

    def test_bad_config_yaml_is_logged(self, caplog, tmp_path):
        """When config.yaml is malformed, a warning should be logged."""
        bad_yaml = tmp_path / "config.yaml"
        bad_yaml.write_text("invalid: yaml: [[[bad")

        job = {
            "id": "test-job",
            "name": "test",
            "prompt": "hello",
        }

        # Mock heavy post-yaml work so the test only exercises the warning
        # path. Without these mocks, _run_job_impl continues into provider
        # resolution and MCP discovery, both of which can spawn subprocesses
        # / hit the network and have caused this test to time out on CI
        # (>30s wall clock) under load. See PR #33661 follow-up.
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   return_value={"provider": "openrouter", "api_key": "x",
                                 "base_url": "https://example.invalid",
                                 "api_mode": "chat_completions"}), \
             patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
                run_job(job)

        assert any("failed to load config.yaml" in r.message for r in caplog.records), \
            f"Expected 'failed to load config.yaml' warning in logs, got: {[r.message for r in caplog.records]}"

    def test_bad_prefill_messages_is_logged(self, caplog, tmp_path):
        """When the prefill messages file contains invalid JSON, a warning should be logged."""
        # Valid config.yaml that points to a bad prefill file
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text("prefill_messages_file: prefill.json\n")

        bad_prefill = tmp_path / "prefill.json"
        bad_prefill.write_text("{not valid json!!!")

        job = {
            "id": "test-job",
            "name": "test",
            "prompt": "hello",
        }

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   return_value={"provider": "openrouter", "api_key": "x",
                                 "base_url": "https://example.invalid",
                                 "api_mode": "chat_completions"}), \
             patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
                run_job(job)

        assert any("failed to parse prefill messages" in r.message for r in caplog.records), \
            f"Expected 'failed to parse prefill messages' warning in logs, got: {[r.message for r in caplog.records]}"


class TestRunJobConfigEnvVarExpansion:
    """Verify that ${VAR} references in config.yaml are expanded when running cron jobs."""

    _RUNTIME = {
        "api_key": "test-key",
        "base_url": "https://example.invalid/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
    }

    def test_model_env_ref_in_config_yaml_is_expanded(self, tmp_path, monkeypatch):
        """${VAR} in config.yaml model: is expanded using env after .env is loaded."""
        (tmp_path / "config.yaml").write_text("model: ${_HERMES_TEST_CRON_MODEL}\n")
        monkeypatch.setenv("_HERMES_TEST_CRON_MODEL", "gpt-4o-mini-cron-test")

        job = {"id": "env-job", "name": "env test", "prompt": "hi"}
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   return_value=self._RUNTIME), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            success, _, _, error = run_job(job)

        assert success is True
        assert error is None
        kwargs = mock_agent_cls.call_args.kwargs
        assert kwargs["model"] == "gpt-4o-mini-cron-test", (
            f"Expected model='gpt-4o-mini-cron-test', got {kwargs['model']!r}. "
            "config.yaml ${VAR} was not expanded in the cron execution path."
        )

    def test_fallback_model_env_ref_in_config_yaml_is_expanded(self, tmp_path, monkeypatch):
        """${VAR} in config.yaml fallback_providers model: is expanded."""
        (tmp_path / "config.yaml").write_text(
            "fallback_providers:\n"
            "  - provider: openrouter\n"
            "    model: ${_HERMES_TEST_CRON_FALLBACK}\n"
        )
        monkeypatch.setenv("_HERMES_TEST_CRON_FALLBACK", "gpt-4o-fallback-test")

        job = {"id": "fb-job", "name": "fallback test", "prompt": "hi"}
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   return_value=self._RUNTIME), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            run_job(job)

        kwargs = mock_agent_cls.call_args.kwargs
        fb = kwargs.get("fallback_model") or []
        fb_list = fb if isinstance(fb, list) else [fb]
        expanded = [e.get("model") for e in fb_list if isinstance(e, dict)]
        assert "gpt-4o-fallback-test" in expanded, (
            f"Expected expanded fallback model in {expanded!r}. "
            "config.yaml ${VAR} in fallback_providers was not expanded."
        )

    def test_unexpanded_ref_passthrough_when_var_unset(self, tmp_path, monkeypatch):
        """When the env var is not set, the literal ${VAR} is kept verbatim (not crashed)."""
        (tmp_path / "config.yaml").write_text("model: ${_HERMES_TEST_CRON_UNSET_VAR}\n")
        monkeypatch.delenv("_HERMES_TEST_CRON_UNSET_VAR", raising=False)

        job = {"id": "unset-job", "name": "unset var test", "prompt": "hi"}
        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                   return_value=self._RUNTIME), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            success, _, _, error = run_job(job)

        assert success is True
        kwargs = mock_agent_cls.call_args.kwargs
        # Unresolved refs are kept verbatim — _expand_env_vars contract
        assert kwargs["model"] == "${_HERMES_TEST_CRON_UNSET_VAR}"


class TestRunJobSkillBacked:
    def test_run_job_preserves_skill_env_passthrough_into_worker_thread(self, tmp_path):
        job = {
            "id": "skill-env-job",
            "name": "skill env test",
            "prompt": "Use the skill.",
            "skill": "notion",
        }

        fake_db = MagicMock()

        def _skill_view(name):
            assert name == "notion"
            from tools.env_passthrough import register_env_passthrough

            register_env_passthrough(["NOTION_API_KEY"])
            return json.dumps({"success": True, "content": "# notion\nUse Notion."})

        def _run_conversation(prompt):
            from tools.env_passthrough import get_all_passthrough

            assert "NOTION_API_KEY" in get_all_passthrough()
            return {"final_response": "ok"}

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("tools.skills_tool.skill_view", side_effect=_skill_view), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.side_effect = _run_conversation
            mock_agent_cls.return_value = mock_agent

            try:
                success, output, final_response, error = run_job(job)
            finally:
                clear_env_passthrough()

        assert success is True
        assert error is None
        assert final_response == "ok"

    def test_run_job_preserves_credential_file_passthrough_into_worker_thread(self, tmp_path):
        """copy_context() also propagates credential_files ContextVar."""
        job = {
            "id": "cred-env-job",
            "name": "cred file test",
            "prompt": "Use the skill.",
            "skill": "google-workspace",
        }

        fake_db = MagicMock()

        # Create a credential file so register_credential_file succeeds
        cred_dir = tmp_path / "credentials"
        cred_dir.mkdir()
        (cred_dir / "google_token.json").write_text('{"token": "t"}')

        def _skill_view(name):
            assert name == "google-workspace"
            from tools.credential_files import register_credential_file

            register_credential_file("credentials/google_token.json")
            return json.dumps({"success": True, "content": "# google-workspace\nUse Google."})

        def _run_conversation(prompt):
            from tools.credential_files import _get_registered

            registered = _get_registered()
            assert registered, "credential files must be visible in worker thread"
            assert any("google_token.json" in v for v in registered.values())
            return {"final_response": "ok"}

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("tools.credential_files._resolve_hermes_home", return_value=tmp_path), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("tools.skills_tool.skill_view", side_effect=_skill_view), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.side_effect = _run_conversation
            mock_agent_cls.return_value = mock_agent

            try:
                success, output, final_response, error = run_job(job)
            finally:
                clear_credential_files()

        assert success is True
        assert error is None
        assert final_response == "ok"

    def test_run_job_loads_skill_and_disables_recursive_cron_tools(self, tmp_path):
        job = {
            "id": "skill-job",
            "name": "skill test",
            "prompt": "Check the feeds and summarize anything new.",
            "skill": "blogwatcher",
        }

        fake_db = MagicMock()

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("tools.skills_tool.skill_view", return_value=json.dumps({"success": True, "content": "# Blogwatcher\nFollow this skill."})), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        assert error is None
        assert final_response == "ok"

        kwargs = mock_agent_cls.call_args.kwargs
        assert "cronjob" in (kwargs["disabled_toolsets"] or [])

        prompt_arg = mock_agent.run_conversation.call_args.args[0]
        assert "blogwatcher" in prompt_arg
        assert "Follow this skill" in prompt_arg
        assert "Check the feeds and summarize anything new." in prompt_arg

    def test_run_job_loads_multiple_skills_in_order(self, tmp_path):
        job = {
            "id": "multi-skill-job",
            "name": "multi skill test",
            "prompt": "Combine the results.",
            "skills": ["blogwatcher", "maps"],
        }

        fake_db = MagicMock()

        def _skill_view(name):
            return json.dumps({"success": True, "content": f"# {name}\nInstructions for {name}."})

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("dotenv.load_dotenv"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "***",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("tools.skills_tool.skill_view", side_effect=_skill_view) as skill_view_mock, \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, output, final_response, error = run_job(job)

        assert success is True
        assert error is None
        assert final_response == "ok"
        assert skill_view_mock.call_count == 2
        assert [call.args[0] for call in skill_view_mock.call_args_list] == ["blogwatcher", "maps"]

        prompt_arg = mock_agent.run_conversation.call_args.args[0]
        assert prompt_arg.index("blogwatcher") < prompt_arg.index("maps")
        assert "Instructions for blogwatcher." in prompt_arg
        assert "Instructions for maps." in prompt_arg
        assert "Combine the results." in prompt_arg


@pytest.mark.usefixtures("_tick_lock_isolated")
class TestSilentDelivery:
    """Verify that [SILENT] responses suppress delivery while still saving output."""

    def _make_job(self):
        return {
            "id": "monitor-job",
            "name": "monitor",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "123"},
        }

    def test_silent_response_suppresses_delivery(self, caplog):
        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._make_job()], [])), \
             patch("cron.scheduler.run_job", return_value=(True, "# output", "[SILENT]", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result") as deliver_mock, \
             patch("cron.scheduler.mark_job_run"):
            from cron.scheduler import tick
            with caplog.at_level(logging.INFO, logger="cron.scheduler"):
                tick(verbose=False)
        deliver_mock.assert_not_called()
        assert any(SILENT_MARKER in r.message for r in caplog.records)

    def test_silent_with_note_suppresses_delivery(self):
        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._make_job()], [])), \
             patch("cron.scheduler.run_job", return_value=(True, "# output", "[SILENT] No changes detected", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result") as deliver_mock, \
             patch("cron.scheduler.mark_job_run"):
            from cron.scheduler import tick
            tick(verbose=False)
        deliver_mock.assert_not_called()

    def test_silent_trailing_suppresses_delivery(self):
        """Agent appended [SILENT] after explanation text — must still suppress."""
        response = "2 deals filtered out (like<10, reply<15).\n\n[SILENT]"
        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._make_job()], [])), \
             patch("cron.scheduler.run_job", return_value=(True, "# output", response, None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result") as deliver_mock, \
             patch("cron.scheduler.mark_job_run"):
            from cron.scheduler import tick
            tick(verbose=False)
        deliver_mock.assert_not_called()

    def test_silent_is_case_insensitive(self):
        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._make_job()], [])), \
             patch("cron.scheduler.run_job", return_value=(True, "# output", "[silent] nothing new", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result") as deliver_mock, \
             patch("cron.scheduler.mark_job_run"):
            from cron.scheduler import tick
            tick(verbose=False)
        deliver_mock.assert_not_called()

    def test_failed_job_always_delivers(self):
        """Failed jobs deliver regardless of [SILENT] in output."""
        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._make_job()], [])), \
             patch("cron.scheduler.run_job", return_value=(False, "# output", "", "some error")), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result") as deliver_mock, \
             patch("cron.scheduler.mark_job_run"):
            from cron.scheduler import tick
            tick(verbose=False)
        deliver_mock.assert_called_once()

    def test_output_saved_even_when_delivery_suppressed(self):
        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._make_job()], [])), \
             patch("cron.scheduler.run_job", return_value=(True, "# full output", "[SILENT]", None)), \
             patch("cron.scheduler.save_job_output") as save_mock, \
             patch("cron.scheduler._deliver_result") as deliver_mock, \
             patch("cron.scheduler.mark_job_run"):
            save_mock.return_value = "/tmp/out.md"
            from cron.scheduler import tick
            tick(verbose=False)
        save_mock.assert_called_once_with("monitor-job", "# full output")
        deliver_mock.assert_not_called()

    def test_whitespace_only_response_is_marked_failed_not_delivered(self):
        """Whitespace-only final responses should behave like empty responses."""
        # v0.15.1 catch-up: tick() consumes get_due_and_skipped_jobs() (upstream),
        # so patch that — the auto-merge had carried the fork's stale get_due_jobs
        # patch, which left tick reading the real (empty) due list (mirrors the
        # sibling test_output_saved above).
        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._make_job()], [])), \
             patch("cron.scheduler.run_job", return_value=(True, "# output", "   \n\t  ", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result") as deliver_mock, \
             patch("cron.scheduler.mark_job_run") as mark_mock:
            from cron.scheduler import tick
            tick(verbose=False)

        deliver_mock.assert_not_called()
        mark_mock.assert_called_once_with(
            "monitor-job",
            False,
            "Agent completed but produced empty response (model error, timeout, or misconfiguration)",
            delivery_error=None,
        )


@pytest.mark.usefixtures("_tick_lock_isolated")
class TestEventEmitterSummary:
    def _make_job(self):
        return {
            "id": "learning-loop",
            "name": "learning-loop",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "123"},
        }

    def test_tick_preserves_moderately_long_summary_for_event_bus(self):
        """Cron event summaries should not be hard-cut at 500 chars."""
        response = (
            "Learning-loop review complete.\n\n"
            "- No `reasoning_effort` changes were justified. Effective live map still holds: "
            "main/sentinel/devflow `high`, matcher/tailor `xhigh`, tracker/applier/cv-handler "
            "`medium`, scout `low`, notifier `minimal`.\n"
            "- No `nudge.interval` changes were needed. Consolidated cadence is still correct: "
            "main/tracker/sentinel `14400`, scout/tailor/cv-handler/devflow `21600`, applier "
            "`10800`, matcher `7200`, notifier `43200`. "
            "`nudge.consolidate_memory: true` is still enabled everywhere."
        )
        assert len(response) > 500

        emitter = MagicMock()
        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._make_job()], [])), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job", return_value=(True, "# output", response, None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result"), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[{"id": "learning-loop", "consecutive_errors": 0}]):
            from cron.scheduler import tick
            tick(verbose=False)

        assert emitter.on_job_completed.call_args.kwargs["output_summary"] == response


class TestBuildJobPromptSilentHint:
    """Verify _build_job_prompt always injects [SILENT] guidance."""

    def test_hint_always_present(self):
        job = {"prompt": "Check for updates"}
        result = _build_job_prompt(job)
        assert "[SILENT]" in result
        assert "Check for updates" in result

    def test_hint_present_even_without_prompt(self):
        job = {"prompt": ""}
        result = _build_job_prompt(job)
        assert "[SILENT]" in result

    def test_hint_present_when_legacy_prompt_is_null(self):
        job = {"id": "abc123deadbe", "name": None, "prompt": None}
        result = _build_job_prompt(job)
        assert "[SILENT]" in result

    def test_delivery_guidance_present(self):
        """Cron hint tells agents their final response is auto-delivered."""
        job = {"prompt": "Generate a report"}
        result = _build_job_prompt(job)
        assert "do NOT use send_message" in result
        assert "automatically delivered" in result

    def test_delivery_guidance_precedes_user_prompt(self):
        """System guidance appears before the user's prompt text."""
        job = {"prompt": "My custom prompt"}
        result = _build_job_prompt(job)
        system_pos = result.index("do NOT use send_message")
        prompt_pos = result.index("My custom prompt")
        assert system_pos < prompt_pos


class TestParseWakeGate:
    """Unit tests for _parse_wake_gate — pure function, no side effects."""

    def test_empty_output_wakes(self):
        from cron.scheduler import _parse_wake_gate
        assert _parse_wake_gate("") is True
        assert _parse_wake_gate(None) is True

    def test_whitespace_only_wakes(self):
        from cron.scheduler import _parse_wake_gate
        assert _parse_wake_gate("   \n\n  \t\n") is True

    def test_non_json_last_line_wakes(self):
        from cron.scheduler import _parse_wake_gate
        assert _parse_wake_gate("hello world") is True
        assert _parse_wake_gate("line 1\nline 2\nplain text") is True

    def test_json_non_dict_wakes(self):
        """Bare arrays, numbers, strings must not be interpreted as a gate."""
        from cron.scheduler import _parse_wake_gate
        assert _parse_wake_gate("[1, 2, 3]") is True
        assert _parse_wake_gate("42") is True
        assert _parse_wake_gate('"wakeAgent"') is True

    def test_wake_gate_false_skips(self):
        from cron.scheduler import _parse_wake_gate
        assert _parse_wake_gate('{"wakeAgent": false}') is False

    def test_wake_gate_true_wakes(self):
        from cron.scheduler import _parse_wake_gate
        assert _parse_wake_gate('{"wakeAgent": true}') is True

    def test_wake_gate_missing_wakes(self):
        """A JSON dict without a wakeAgent key defaults to waking."""
        from cron.scheduler import _parse_wake_gate
        assert _parse_wake_gate('{"data": {"foo": "bar"}}') is True

    def test_non_boolean_false_still_wakes(self):
        """Only strict ``False`` skips — truthy/falsy shortcuts are too risky."""
        from cron.scheduler import _parse_wake_gate
        assert _parse_wake_gate('{"wakeAgent": 0}') is True
        assert _parse_wake_gate('{"wakeAgent": null}') is True
        assert _parse_wake_gate('{"wakeAgent": ""}') is True

    def test_only_last_non_empty_line_parsed(self):
        from cron.scheduler import _parse_wake_gate
        multi = 'some log output\nmore output\n{"wakeAgent": false}'
        assert _parse_wake_gate(multi) is False

    def test_trailing_blank_lines_ignored(self):
        from cron.scheduler import _parse_wake_gate
        multi = '{"wakeAgent": false}\n\n\n'
        assert _parse_wake_gate(multi) is False

    def test_non_last_json_line_does_not_gate(self):
        """A JSON gate on an earlier line with plain text after it does NOT trigger."""
        from cron.scheduler import _parse_wake_gate
        multi = '{"wakeAgent": false}\nactually this is the real output'
        assert _parse_wake_gate(multi) is True


class TestRunJobWakeGate:
    """Integration tests for run_job wake-gate short-circuit."""

    @pytest.fixture(autouse=True)
    def _stub_runtime_provider(self):
        """Stub ``resolve_runtime_provider`` for wake-gate tests.

        ``run_job`` resolves the runtime provider BEFORE constructing
        ``AIAgent``, so these tests must mock ``resolve_runtime_provider``
        in addition to ``AIAgent`` — otherwise in a hermetic CI env (no
        API keys), the resolver raises and the test fails before the
        patched AIAgent is ever reached.
        """
        fake_runtime = {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
            "source": "stub",
            "requested_provider": None,
        }
        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=fake_runtime,
        ):
            yield

    def _make_job(self, name="wake-gate-test", script="check.py"):
        """Minimal valid cron job dict for run_job."""
        return {
            "id": f"job_{name}",
            "name": name,
            "prompt": "Do a thing",
            "schedule": "*/5 * * * *",
            "script": script,
        }

    def test_wake_false_skips_agent_and_returns_silent(self, caplog):
        """When _run_job_script output ends with {wakeAgent: false}, the agent
        is not invoked and run_job returns the SILENT marker so delivery is
        suppressed."""
        from cron.scheduler import SILENT_MARKER
        import cron.scheduler as scheduler

        with patch.object(scheduler, "_run_job_script",
                          return_value=(True, '{"wakeAgent": false}')), \
             patch("run_agent.AIAgent") as agent_cls:
            success, doc, final, err = scheduler.run_job(self._make_job())

        assert success is True
        assert err is None
        assert final == SILENT_MARKER
        assert "Script gate returned `wakeAgent=false`" in doc
        agent_cls.assert_not_called()

    def test_wake_true_runs_agent_with_injected_output(self):
        """When the script returns {wakeAgent: true, data: ...}, the agent is
        invoked and the data line still shows up in the prompt."""
        import cron.scheduler as scheduler

        script_output = '{"wakeAgent": true, "data": {"new": 3}}'
        agent = MagicMock()
        agent.run_conversation = MagicMock(return_value={
            "final_response": "ok", "messages": []
        })
        with patch.object(scheduler, "_run_job_script",
                          return_value=(True, script_output)), \
             patch("run_agent.AIAgent", return_value=agent) as agent_cls:
            success, doc, final, err = scheduler.run_job(self._make_job())

        agent_cls.assert_called_once()
        # The script output should be visible in the prompt passed to
        # run_conversation.
        call_kwargs = agent.run_conversation.call_args
        prompt_arg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("user_message", "")
        assert script_output in prompt_arg
        assert success is True
        assert err is None

    def test_script_runs_only_once_on_wake(self):
        """Wake-true path must not re-run the script inside _build_job_prompt
        (script would execute twice otherwise, wasting work and risking
        double-side-effects)."""
        import cron.scheduler as scheduler

        call_count = 0
        def _script_stub(path):
            nonlocal call_count
            call_count += 1
            return (True, "regular output")

        agent = MagicMock()
        agent.run_conversation = MagicMock(return_value={
            "final_response": "ok", "messages": []
        })
        with patch.object(scheduler, "_run_job_script", side_effect=_script_stub), \
             patch("run_agent.AIAgent", return_value=agent):
            scheduler.run_job(self._make_job())

        assert call_count == 1, f"script ran {call_count}x, expected exactly 1"

    def test_script_failure_does_not_trigger_gate(self):
        """If _run_job_script returns success=False, the gate is NOT evaluated
        and the agent still runs (the failure is reported as context)."""
        import cron.scheduler as scheduler

        # Malicious or broken script whose stderr happens to contain the
        # gate JSON — we must NOT honor it because ran_ok is False.
        agent = MagicMock()
        agent.run_conversation = MagicMock(return_value={
            "final_response": "ok", "messages": []
        })
        with patch.object(scheduler, "_run_job_script",
                          return_value=(False, '{"wakeAgent": false}')), \
             patch("run_agent.AIAgent", return_value=agent) as agent_cls:
            success, doc, final, err = scheduler.run_job(self._make_job())

        agent_cls.assert_called_once()  # Agent DID wake despite the gate-like text

    def test_no_script_path_runs_agent_normally(self):
        """Regression: jobs without a script still work."""
        import cron.scheduler as scheduler

        agent = MagicMock()
        agent.run_conversation = MagicMock(return_value={
            "final_response": "ok", "messages": []
        })
        job = self._make_job(script=None)
        job.pop("script", None)
        with patch.object(scheduler, "_run_job_script") as script_fn, \
             patch("run_agent.AIAgent", return_value=agent) as agent_cls:
            scheduler.run_job(job)

        script_fn.assert_not_called()
        agent_cls.assert_called_once()


class TestBuildJobPromptMissingSkill:
    """Verify that a missing skill logs a warning and does not crash the job."""

    def _missing_skill_view(self, name: str) -> str:
        return json.dumps({"success": False, "error": f"Skill '{name}' not found."})

    def test_missing_skill_does_not_raise(self):
        """Job should run even when a referenced skill is not installed."""
        with patch("tools.skills_tool.skill_view", side_effect=self._missing_skill_view):
            result = _build_job_prompt({"skills": ["ghost-skill"], "prompt": "do something"})
        # prompt is preserved even though skill was skipped
        assert "do something" in result

    def test_missing_skill_injects_user_notice_into_prompt(self):
        """A system notice about the missing skill is injected into the prompt."""
        with patch("tools.skills_tool.skill_view", side_effect=self._missing_skill_view):
            result = _build_job_prompt({"skills": ["ghost-skill"], "prompt": "do something"})
        assert "ghost-skill" in result
        assert "not found" in result.lower() or "skipped" in result.lower()

    def test_missing_skill_logs_warning(self, caplog):
        """A warning is logged when a skill cannot be found."""
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            with patch("tools.skills_tool.skill_view", side_effect=self._missing_skill_view):
                _build_job_prompt({"name": "My Job", "skills": ["ghost-skill"], "prompt": "do something"})
        assert any("ghost-skill" in record.message for record in caplog.records)

    def test_valid_skill_loaded_alongside_missing(self):
        """A valid skill is still loaded when another skill in the list is missing."""

        def _mixed_skill_view(name: str) -> str:
            if name == "real-skill":
                return json.dumps({"success": True, "content": "Real skill content."})
            return json.dumps({"success": False, "error": f"Skill '{name}' not found."})

        with patch("tools.skills_tool.skill_view", side_effect=_mixed_skill_view):
            result = _build_job_prompt({"skills": ["ghost-skill", "real-skill"], "prompt": "go"})
        assert "Real skill content." in result
        assert "go" in result


class TestBuildJobPromptBumpUse:
    """Verify that cron jobs bump skill usage counters so the curator sees them as active."""

    def test_bump_use_called_for_loaded_skill(self):
        """bump_use is called for each successfully loaded skill."""

        def _skill_view(name: str) -> str:
            return json.dumps({"success": True, "content": f"Content for {name}."})

        with patch("tools.skills_tool.skill_view", side_effect=_skill_view), \
             patch("tools.skill_usage.bump_use") as mock_bump:
            _build_job_prompt({"skills": ["alpha", "beta"], "prompt": "go"})

        assert mock_bump.call_count == 2
        calls = [c[0][0] for c in mock_bump.call_args_list]
        assert "alpha" in calls
        assert "beta" in calls

    def test_bump_use_not_called_for_missing_skill(self):
        """bump_use is NOT called when a skill fails to load."""

        def _missing_view(name: str) -> str:
            return json.dumps({"success": False, "error": "not found"})

        with patch("tools.skills_tool.skill_view", side_effect=_missing_view), \
             patch("tools.skill_usage.bump_use") as mock_bump:
            _build_job_prompt({"skills": ["ghost"], "prompt": "go"})

        assert mock_bump.call_count == 0

    def test_bump_failure_does_not_break_prompt(self, caplog):
        """If bump_use raises, the prompt still builds — error is logged at DEBUG."""

        def _skill_view(name: str) -> str:
            return json.dumps({"success": True, "content": "Works."})

        with patch("tools.skills_tool.skill_view", side_effect=_skill_view), \
             patch("tools.skill_usage.bump_use", side_effect=RuntimeError("boom")), \
             caplog.at_level(logging.DEBUG, logger="cron.scheduler"):
            result = _build_job_prompt({"skills": ["good-skill"], "prompt": "go"})

        # Prompt should still contain the skill content and original instruction
        assert "Works." in result
        assert "go" in result
        # The error should be logged at DEBUG level, not crash
        assert any("failed to bump" in r.message for r in caplog.records)


class TestSendMediaViaAdapter:
    """Unit tests for _send_media_via_adapter — routes files to typed adapter methods."""

    def _safe_media_path(self, tmp_path, monkeypatch, name, data=b"media"):
        root = tmp_path / "media-cache"
        media_file = root / name
        media_file.parent.mkdir(parents=True, exist_ok=True)
        media_file.write_bytes(data)
        monkeypatch.setattr(
            "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
            (root,),
        )
        return media_file.resolve()

    @staticmethod
    def _run_with_loop(adapter, chat_id, media_files, metadata, job):
        """Helper: run _send_media_via_adapter with immediate scheduling."""
        from concurrent.futures import Future

        def fake_run_coro(coro, _loop):
            coro.close()
            completed = Future()
            completed.set_result(MagicMock(success=True))
            return completed

        with patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
            _send_media_via_adapter(adapter, chat_id, media_files, metadata, MagicMock(), job)

    def test_video_dispatched_to_send_video(self, tmp_path, monkeypatch):
        adapter = MagicMock()
        adapter.send_video = AsyncMock()
        media_path = self._safe_media_path(tmp_path, monkeypatch, "clip.mp4")
        media_files = [(str(media_path), False)]
        self._run_with_loop(adapter, "123", media_files, None, {"id": "j1"})
        adapter.send_video.assert_called_once()
        assert adapter.send_video.call_args[1]["video_path"] == str(media_path)

    def test_unknown_ext_dispatched_to_send_document(self, tmp_path, monkeypatch):
        adapter = MagicMock()
        adapter.send_document = AsyncMock()
        media_path = self._safe_media_path(tmp_path, monkeypatch, "report.pdf")
        media_files = [(str(media_path), False)]
        self._run_with_loop(adapter, "123", media_files, None, {"id": "j2"})
        adapter.send_document.assert_called_once()
        assert adapter.send_document.call_args[1]["file_path"] == str(media_path)

    def test_multiple_media_files_all_delivered(self, tmp_path, monkeypatch):
        adapter = MagicMock()
        adapter.send_voice = AsyncMock()
        adapter.send_image_file = AsyncMock()
        voice_path = self._safe_media_path(tmp_path, monkeypatch, "voice.mp3")
        photo_path = self._safe_media_path(tmp_path, monkeypatch, "photo.jpg")
        media_files = [(str(voice_path), False), (str(photo_path), False)]
        self._run_with_loop(adapter, "123", media_files, None, {"id": "j3"})
        adapter.send_voice.assert_called_once()
        adapter.send_image_file.assert_called_once()


class TestParallelTick:
    """Verify that tick() runs due jobs concurrently and isolates ContextVars."""

    @pytest.fixture(autouse=True)
    def _isolate_tick_lock(self, tmp_path):
        """Point the tick file lock at a per-test temp dir to avoid xdist contention."""
        lock_dir = tmp_path / "cron"
        lock_dir.mkdir()
        lock_file = lock_dir / ".tick.lock"
        with patch("cron.scheduler._get_lock_paths", return_value=(lock_dir, lock_file)):
            yield

    def test_parallel_jobs_run_concurrently(self):
        """Two jobs launched in the same tick should overlap in time."""
        import threading
        import time

        barrier = threading.Barrier(2, timeout=5)
        call_order = []

        def mock_run_job(job):
            """Each job hits a barrier — both must be active simultaneously."""
            call_order.append(("start", job["id"]))
            barrier.wait()  # blocks until both threads reach here
            call_order.append(("end", job["id"]))
            return (True, "output", "response", None)

        jobs = [
            {"id": "job-a", "name": "a", "deliver": "local"},
            {"id": "job-b", "name": "b", "deliver": "local"},
        ]

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=(jobs, [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler.run_job", side_effect=mock_run_job), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"):
            from cron.scheduler import tick
            result = tick(verbose=False)

        assert result == 2
        # Both starts happened before both ends — proof of concurrency
        starts = [i for i, (action, _) in enumerate(call_order) if action == "start"]
        ends = [i for i, (action, _) in enumerate(call_order) if action == "end"]
        assert len(starts) == 2
        assert len(ends) == 2
        assert max(starts) < min(ends), f"Jobs not concurrent: {call_order}"

    def test_parallel_jobs_isolated_contextvars(self):
        """Each job's ContextVars must be isolated — no cross-contamination."""
        from gateway.session_context import get_session_env
        seen = {}

        def mock_run_job(job):
            origin = job.get("origin", {})
            # run_job sets ContextVars — verify each job sees its own
            from gateway.session_context import set_session_vars, clear_session_vars
            tokens = set_session_vars(
                platform=origin.get("platform", ""),
                chat_id=str(origin.get("chat_id", "")),
            )
            import time
            time.sleep(0.05)  # give other thread time to set its vars
            platform = get_session_env("HERMES_SESSION_PLATFORM")
            chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
            seen[job["id"]] = {"platform": platform, "chat_id": chat_id}
            clear_session_vars(tokens)
            return (True, "output", "response", None)

        jobs = [
            {"id": "tg-job", "name": "tg", "deliver": "local",
             "origin": {"platform": "telegram", "chat_id": "111"}},
            {"id": "dc-job", "name": "dc", "deliver": "local",
             "origin": {"platform": "discord", "chat_id": "222"}},
        ]

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=(jobs, [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler.run_job", side_effect=mock_run_job), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"):
            from cron.scheduler import tick
            tick(verbose=False)

        assert seen["tg-job"] == {"platform": "telegram", "chat_id": "111"}
        assert seen["dc-job"] == {"platform": "discord", "chat_id": "222"}

    def test_max_parallel_env_var(self, monkeypatch):
        """HERMES_CRON_MAX_PARALLEL=1 should restore serial behaviour."""
        monkeypatch.setenv("HERMES_CRON_MAX_PARALLEL", "1")
        call_times = []

        def mock_run_job(job):
            import time
            call_times.append(("start", job["id"], time.monotonic()))
            time.sleep(0.05)
            call_times.append(("end", job["id"], time.monotonic()))
            return (True, "output", "response", None)

        jobs = [
            {"id": "s1", "name": "s1", "deliver": "local"},
            {"id": "s2", "name": "s2", "deliver": "local"},
        ]

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=(jobs, [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler.run_job", side_effect=mock_run_job), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"):
            from cron.scheduler import tick
            result = tick(verbose=False)

        assert result == 2
        # With max_workers=1, second job starts after first ends
        end_s1 = [t for action, jid, t in call_times if action == "end" and jid == "s1"][0]
        start_s2 = [t for action, jid, t in call_times if action == "start" and jid == "s2"][0]
        assert start_s2 >= end_s1, "Jobs ran concurrently despite max_parallel=1"


class TestDeliverResultTimeoutCancelsFuture:
    """When future.result(timeout=60) raises TimeoutError in the live
    adapter delivery path, _deliver_result must cancel the orphan
    coroutine so it cannot duplicate-send after the standalone fallback.
    """

    def test_live_adapter_timeout_cancels_future_and_falls_back(self):
        """End-to-end: live adapter hangs past the 60s budget, _deliver_result
        patches the timeout down to a fast value, confirms future.cancel() fires,
        and verifies the standalone fallback path still delivers."""
        from gateway.config import Platform
        from concurrent.futures import Future

        # Live adapter whose send() coroutine never resolves within the budget
        adapter = AsyncMock()
        adapter.send.return_value = MagicMock(success=True)

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        loop = MagicMock()
        loop.is_running.return_value = True

        # A real concurrent.futures.Future so .cancel() has real semantics,
        # but we override .result() to raise TimeoutError exactly like the
        # 60s wait firing in production.
        captured_future = Future()
        cancel_calls = []
        original_cancel = captured_future.cancel

        def tracking_cancel():
            cancel_calls.append(True)
            return original_cancel()

        captured_future.cancel = tracking_cancel
        captured_future.result = MagicMock(side_effect=TimeoutError("timed out"))

        def fake_run_coro(coro, _loop):
            coro.close()
            return captured_future

        job = {
            "id": "timeout-job",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "123"},
        }

        standalone_send = AsyncMock(return_value={"success": True})

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro), \
             patch("tools.send_message_tool._send_to_platform", new=standalone_send):
            result = _deliver_result(
                job,
                "Hello world",
                adapters={Platform.TELEGRAM: adapter},
                loop=loop,
            )

        # 1. The orphan future was cancelled on timeout (the bug fix)
        assert cancel_calls == [True], "future.cancel() must fire on TimeoutError"
        # 2. The standalone fallback delivered — no double send, no silent drop
        assert result is None, f"expected successful delivery, got error: {result!r}"
        standalone_send.assert_awaited_once()

    def test_live_adapter_thread_fallback_records_delivery_error(self):
        """A cron target with an explicit topic must not be marked clean if
        Telegram falls back to the base chat after "thread not found".
        """
        from gateway.config import Platform
        from gateway.platforms.base import SendResult
        from concurrent.futures import Future

        send_result = SendResult(
            success=True,
            message_id="42",
            raw_response={
                "requested_thread_id": 7072,
                "thread_fallback": True,
            },
        )
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=send_result)

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        loop = MagicMock()
        loop.is_running.return_value = True

        job = {
            "id": "thread-fallback-job",
            "deliver": "telegram:226252250:7072",
        }

        completed_future = Future()
        completed_future.set_result(send_result)

        def fake_run_coro(coro, _loop):
            coro.close()
            return completed_future

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
            result = _deliver_result(
                job,
                "Hello world",
                adapters={Platform.TELEGRAM: adapter},
                loop=loop,
            )

        assert result == (
            "configured thread_id 7072 for telegram:226252250 was not found; "
            "delivered without thread_id"
        )
        adapter.send.assert_called_once_with(
            "226252250",
            "Hello world",
            metadata={"thread_id": "7072"},
        )


class TestSendMediaTimeoutCancelsFuture:
    """Same orphan-coroutine guarantee for _send_media_via_adapter's
    future.result(timeout=30) call. If this times out mid-batch, the
    in-flight coroutine must be cancelled before the next file is tried.
    """

    def test_media_send_timeout_cancels_future_and_continues(self, tmp_path, monkeypatch):
        """End-to-end: _send_media_via_adapter with a future whose .result()
        raises TimeoutError. Assert cancel() fires and the loop proceeds
        to the next file rather than hanging or crashing."""
        from concurrent.futures import Future

        adapter = MagicMock()
        adapter.send_image_file = AsyncMock()
        adapter.send_video = AsyncMock()

        # First file: future that times out. Second file: future that resolves OK.
        timeout_future = Future()
        timeout_cancel_calls = []
        original_cancel = timeout_future.cancel

        def tracking_cancel():
            timeout_cancel_calls.append(True)
            return original_cancel()

        timeout_future.cancel = tracking_cancel
        timeout_future.result = MagicMock(side_effect=TimeoutError("timed out"))

        ok_future = Future()
        ok_future.set_result(MagicMock(success=True))

        futures_iter = iter([timeout_future, ok_future])

        def fake_run_coro(coro, _loop):
            coro.close()
            return next(futures_iter)

        root = tmp_path / "media-cache"
        slow = root / "slow.png"
        fast = root / "fast.mp4"
        slow.parent.mkdir(parents=True)
        slow.write_bytes(b"slow")
        fast.write_bytes(b"fast")
        monkeypatch.setattr(
            "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
            (root,),
        )
        media_files = [
            (str(slow), False),   # times out
            (str(fast), False),   # succeeds
        ]

        loop = MagicMock()
        job = {"id": "media-timeout"}

        with patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
            # Should not raise — the except Exception clause swallows the timeout
            _send_media_via_adapter(adapter, "chat-1", media_files, None, loop, job)

        # 1. The timed-out future was cancelled (the bug fix)
        assert timeout_cancel_calls == [True], "future.cancel() must fire on TimeoutError"
        # 2. Second file still got dispatched — one timeout doesn't abort the batch
        adapter.send_video.assert_called_once()
        assert adapter.send_video.call_args[1]["video_path"] == str(fast.resolve())


# ============================================================================
# Tailor structured iteration event (2026-04-29)
# Plan: docs/superpowers/plans/2026-04-29-tailor-structured-iteration-event.md
# ============================================================================


class TestStripIterationMarkers:
    """Unit tests for _strip_iteration_markers — pure function, no side effects.

    Guards the cron-delivery sanitizer that keeps internal iteration-tracking
    blocks out of user-facing message bodies (Telegram HTML-parse-fallback
    log spam, raw-JSON noise)."""

    def test_no_marker_returned_unchanged(self):
        # Critical no-op guarantee: a normal delivery (incl. trailing
        # whitespace and legit angle brackets like "5 < 10") is untouched.
        text = "All clear — 5 < 10 jobs left.\n\nNothing to do.\n"
        assert _strip_iteration_markers(text) == text

    def test_strips_agent_marker(self):
        text = (
            "Scanned 4 sources.\n\n"
            '<AGENT_ITERATION_JSON>{"agent": "scout", "summary": "ok"}</AGENT_ITERATION_JSON>'
        )
        assert _strip_iteration_markers(text) == "Scanned 4 sources."

    def test_strips_legacy_tailor_marker(self):
        text = (
            "Tailored some.\n\n"
            '<TAILOR_ITERATION_JSON>{"eligible_count": 1}</TAILOR_ITERATION_JSON>'
        )
        assert _strip_iteration_markers(text) == "Tailored some."

    def test_strips_multiline_json_block(self):
        text = (
            "Done.\n\n"
            "<AGENT_ITERATION_JSON>\n"
            "{\n"
            '  "agent": "matcher",\n'
            '  "summary": "scored 12"\n'
            "}\n"
            "</AGENT_ITERATION_JSON>"
        )
        assert _strip_iteration_markers(text) == "Done."

    def test_strips_both_markers_if_present(self):
        text = (
            "Report.\n\n"
            '<AGENT_ITERATION_JSON>{"agent": "a", "summary": "s"}</AGENT_ITERATION_JSON>\n'
            '<TAILOR_ITERATION_JSON>{"eligible_count": 0}</TAILOR_ITERATION_JSON>'
        )
        result = _strip_iteration_markers(text)
        assert "ITERATION_JSON" not in result
        assert result == "Report."

    def test_marker_in_middle_collapses_blank_gap(self):
        text = (
            "Header line.\n\n"
            '<AGENT_ITERATION_JSON>{"agent": "a", "summary": "s"}</AGENT_ITERATION_JSON>\n\n'
            "Footer line."
        )
        # No triple-newline gap should remain where the block was removed.
        result = _strip_iteration_markers(text)
        assert "ITERATION_JSON" not in result
        assert "\n\n\n" not in result
        assert result == "Header line.\n\nFooter line."

    def test_empty_and_none_inputs(self):
        assert _strip_iteration_markers("") == ""
        assert _strip_iteration_markers(None) is None


class TestExtractTailorIteration:
    """Unit tests for _extract_tailor_iteration — pure function, no side effects."""

    def _valid_payload_json(self) -> str:
        return (
            '{"eligible_count": 47, "tailored_count": 0, '
            '"skipped_terminal_count": 47, "skipped_other_count": 0, '
            '"reason": "all_already_terminal"}'
        )

    def test_happy_path_returns_parsed_payload(self):
        from cron.scheduler import _extract_tailor_iteration
        text = (
            "Some preamble.\n\n"
            f"<TAILOR_ITERATION_JSON>\n{self._valid_payload_json()}\n</TAILOR_ITERATION_JSON>\n\n"
            "[SILENT]"
        )
        parsed, err, raw = _extract_tailor_iteration(text)
        assert err is None
        assert parsed is not None
        assert parsed["eligible_count"] == 47
        assert parsed["tailored_count"] == 0
        assert parsed["reason"] == "all_already_terminal"
        assert raw is not None and "all_already_terminal" in raw

    def test_happy_path_block_anywhere_in_response(self):
        """Block can appear before or after free-text — order-insensitive."""
        from cron.scheduler import _extract_tailor_iteration
        text = (
            "Tailored 0 of 47 packets.\n\n"
            "All 47 were already terminal (already-applied).\n\n"
            f"<TAILOR_ITERATION_JSON>{self._valid_payload_json()}</TAILOR_ITERATION_JSON>"
        )
        parsed, err, _ = _extract_tailor_iteration(text)
        assert err is None
        assert parsed["eligible_count"] == 47

    def test_missing_marker_returns_missing_reason(self):
        from cron.scheduler import (
            _extract_tailor_iteration,
            TAILOR_ITERATION_REASON_MISSING,
        )
        parsed, err, raw = _extract_tailor_iteration("Tailored zero packets. [SILENT]")
        assert parsed is None
        assert err == TAILOR_ITERATION_REASON_MISSING
        assert raw is None

    def test_empty_input_returns_missing(self):
        from cron.scheduler import (
            _extract_tailor_iteration,
            TAILOR_ITERATION_REASON_MISSING,
        )
        parsed, err, _ = _extract_tailor_iteration("")
        assert err == TAILOR_ITERATION_REASON_MISSING
        parsed, err, _ = _extract_tailor_iteration(None)
        assert err == TAILOR_ITERATION_REASON_MISSING

    def test_malformed_json_returns_parse_failed(self):
        from cron.scheduler import (
            _extract_tailor_iteration,
            TAILOR_ITERATION_REASON_PARSE_FAILED,
        )
        text = "<TAILOR_ITERATION_JSON>this is not json {[ </TAILOR_ITERATION_JSON>"
        parsed, err, raw = _extract_tailor_iteration(text)
        assert parsed is None
        assert err == TAILOR_ITERATION_REASON_PARSE_FAILED
        # raw_block must be present so the AGENT_ERROR payload can include it.
        assert raw and "this is not json" in raw

    def test_schema_mismatch_missing_field(self):
        from cron.scheduler import (
            _extract_tailor_iteration,
            TAILOR_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        # Missing reason
        text = (
            '<TAILOR_ITERATION_JSON>{"eligible_count": 1, "tailored_count": 0, '
            '"skipped_terminal_count": 1, "skipped_other_count": 0}'
            "</TAILOR_ITERATION_JSON>"
        )
        parsed, err, raw = _extract_tailor_iteration(text)
        assert parsed is None
        assert err == TAILOR_ITERATION_REASON_SCHEMA_MISMATCH
        assert raw is not None  # raw block kept for AGENT_ERROR detail

    def test_schema_mismatch_negative_count(self):
        from cron.scheduler import (
            _extract_tailor_iteration,
            TAILOR_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = (
            '<TAILOR_ITERATION_JSON>{"eligible_count": -1, "tailored_count": 0, '
            '"skipped_terminal_count": 0, "skipped_other_count": 0, '
            '"reason": "no_eligible_packets"}</TAILOR_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_tailor_iteration(text)
        assert err == TAILOR_ITERATION_REASON_SCHEMA_MISMATCH

    def test_schema_mismatch_count_is_string(self):
        from cron.scheduler import (
            _extract_tailor_iteration,
            TAILOR_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = (
            '<TAILOR_ITERATION_JSON>{"eligible_count": "47", "tailored_count": 0, '
            '"skipped_terminal_count": 0, "skipped_other_count": 0, '
            '"reason": "tailored_some"}</TAILOR_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_tailor_iteration(text)
        assert err == TAILOR_ITERATION_REASON_SCHEMA_MISMATCH

    def test_schema_mismatch_count_is_bool(self):
        """bool subclasses int but masquerading as count is unhelpful."""
        from cron.scheduler import (
            _extract_tailor_iteration,
            TAILOR_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = (
            '<TAILOR_ITERATION_JSON>{"eligible_count": true, "tailored_count": 0, '
            '"skipped_terminal_count": 0, "skipped_other_count": 0, '
            '"reason": "other"}</TAILOR_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_tailor_iteration(text)
        assert err == TAILOR_ITERATION_REASON_SCHEMA_MISMATCH

    def test_schema_mismatch_reason_empty(self):
        from cron.scheduler import (
            _extract_tailor_iteration,
            TAILOR_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = (
            '<TAILOR_ITERATION_JSON>{"eligible_count": 0, "tailored_count": 0, '
            '"skipped_terminal_count": 0, "skipped_other_count": 0, '
            '"reason": ""}</TAILOR_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_tailor_iteration(text)
        assert err == TAILOR_ITERATION_REASON_SCHEMA_MISMATCH

    def test_schema_mismatch_non_dict_payload(self):
        """JSON parses but is a list — must be flagged as schema mismatch."""
        from cron.scheduler import (
            _extract_tailor_iteration,
            TAILOR_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = "<TAILOR_ITERATION_JSON>[1, 2, 3]</TAILOR_ITERATION_JSON>"
        parsed, err, _ = _extract_tailor_iteration(text)
        assert err == TAILOR_ITERATION_REASON_SCHEMA_MISMATCH

    def test_unknown_reason_is_passed_through(self):
        """Unknown enum values are not the parser's concern — Critic flags drift."""
        from cron.scheduler import _extract_tailor_iteration
        text = (
            '<TAILOR_ITERATION_JSON>{"eligible_count": 1, "tailored_count": 1, '
            '"skipped_terminal_count": 0, "skipped_other_count": 0, '
            '"reason": "bizarre_new_reason"}</TAILOR_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_tailor_iteration(text)
        assert err is None
        assert parsed["reason"] == "bizarre_new_reason"

    def test_marker_with_surrounding_whitespace(self):
        """Multiline JSON between markers, common LLM output shape."""
        from cron.scheduler import _extract_tailor_iteration
        text = """\
Tailoring run complete.

<TAILOR_ITERATION_JSON>
{
  "eligible_count": 4,
  "tailored_count": 2,
  "skipped_terminal_count": 1,
  "skipped_other_count": 1,
  "reason": "mixed"
}
</TAILOR_ITERATION_JSON>

Tailored 2 of 4 packets. Diego, see applications/.
"""
        parsed, err, _ = _extract_tailor_iteration(text)
        assert err is None
        assert parsed["tailored_count"] == 2


class TestEmitTailorIterationEvent:
    """Behavior tests for _emit_tailor_iteration_event — mocked EventBus.

    Verifies the gating, event-type selection, and payload shape on each
    of the 4 cases from the design doc.
    """

    def _make_emitter_with_bus(self):
        emitter = MagicMock()
        emitter.bus = MagicMock()
        return emitter

    def _tailor_job(self):
        return {"id": "jobflow-tailor-123", "name": "jobflow-tailor"}

    def test_gated_to_jobflow_tailor_only(self):
        """Other crons must not fire any tailor_iteration / AGENT_ERROR emit."""
        from cron.scheduler import _emit_tailor_iteration_event
        emitter = self._make_emitter_with_bus()
        not_tailor = {"id": "matcher-shadow", "name": "matcher-shadow"}
        _emit_tailor_iteration_event(emitter, not_tailor, "no marker, but not gated")
        assert emitter.bus.emit.call_count == 0

    def test_no_emitter_short_circuits(self):
        """None emitter must not raise (cron is in early-startup state)."""
        from cron.scheduler import _emit_tailor_iteration_event
        # Should not raise
        _emit_tailor_iteration_event(None, self._tailor_job(), "")

    def test_happy_path_emits_tailor_iteration_event(self):
        from cron.scheduler import _emit_tailor_iteration_event
        from events.schema import EventType
        emitter = self._make_emitter_with_bus()
        response = (
            'Tailored 2 of 4 packets.\n'
            '<TAILOR_ITERATION_JSON>'
            '{"eligible_count": 4, "tailored_count": 2, '
            '"skipped_terminal_count": 1, "skipped_other_count": 1, '
            '"reason": "mixed"}'
            '</TAILOR_ITERATION_JSON>'
        )
        _emit_tailor_iteration_event(emitter, self._tailor_job(), response)
        assert emitter.bus.emit.call_count == 1
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.TAILOR_ITERATION
        assert kwargs["source"] == "tailor"
        assert kwargs["correlation_id"] == "jobflow-tailor-123"
        assert kwargs["job_id"] == "jobflow-tailor-123"
        payload = kwargs["payload"]
        assert payload["eligible_count"] == 4
        assert payload["tailored_count"] == 2
        assert payload["reason"] == "mixed"
        # Plus the metadata we add in the wrapper
        assert payload["job_name"] == "jobflow-tailor"
        assert payload["job_id"] == "jobflow-tailor-123"

    def test_missing_marker_emits_agent_error_with_reason(self):
        from cron.scheduler import (
            _emit_tailor_iteration_event,
            TAILOR_ITERATION_REASON_MISSING,
        )
        from events.schema import EventType
        emitter = self._make_emitter_with_bus()
        _emit_tailor_iteration_event(emitter, self._tailor_job(), "Tailored 0 packets. [SILENT]")
        assert emitter.bus.emit.call_count == 1
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.AGENT_ERROR
        assert kwargs["source"] == "tailor"
        assert kwargs["payload"]["reason"] == TAILOR_ITERATION_REASON_MISSING
        # No raw block kept for the missing case (nothing to keep)
        assert "detail" not in kwargs["payload"]

    def test_malformed_json_emits_agent_error_parse_failed(self):
        from cron.scheduler import (
            _emit_tailor_iteration_event,
            TAILOR_ITERATION_REASON_PARSE_FAILED,
        )
        from events.schema import EventType
        emitter = self._make_emitter_with_bus()
        response = (
            "<TAILOR_ITERATION_JSON>this is not json {[ </TAILOR_ITERATION_JSON>"
        )
        _emit_tailor_iteration_event(emitter, self._tailor_job(), response)
        assert emitter.bus.emit.call_count == 1
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.AGENT_ERROR
        assert kwargs["payload"]["reason"] == TAILOR_ITERATION_REASON_PARSE_FAILED
        # Malformed block kept (bounded) so operators can see it
        assert "detail" in kwargs["payload"]
        assert "this is not json" in kwargs["payload"]["detail"]

    def test_schema_mismatch_emits_agent_error(self):
        from cron.scheduler import (
            _emit_tailor_iteration_event,
            TAILOR_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        from events.schema import EventType
        emitter = self._make_emitter_with_bus()
        # Missing required field
        response = (
            '<TAILOR_ITERATION_JSON>{"eligible_count": 1}</TAILOR_ITERATION_JSON>'
        )
        _emit_tailor_iteration_event(emitter, self._tailor_job(), response)
        assert emitter.bus.emit.call_count == 1
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.AGENT_ERROR
        assert kwargs["payload"]["reason"] == TAILOR_ITERATION_REASON_SCHEMA_MISMATCH

    def test_emit_failure_does_not_propagate(self):
        """A bus.emit() exception must degrade to debug log, not crash tick."""
        from cron.scheduler import _emit_tailor_iteration_event
        emitter = MagicMock()
        emitter.bus = MagicMock()
        emitter.bus.emit.side_effect = RuntimeError("simulated DB lock")
        # Must not raise
        _emit_tailor_iteration_event(emitter, self._tailor_job(), "anything")

    def test_detail_block_truncated_at_2000_chars(self):
        """Bounded detail prevents multi-MB payload blowing up audit log."""
        from cron.scheduler import _emit_tailor_iteration_event
        emitter = self._make_emitter_with_bus()
        big = "x" * 5000
        response = f"<TAILOR_ITERATION_JSON>{big}</TAILOR_ITERATION_JSON>"
        _emit_tailor_iteration_event(emitter, self._tailor_job(), response)
        kwargs = emitter.bus.emit.call_args.kwargs
        assert len(kwargs["payload"]["detail"]) == 2000

    def test_correlation_id_falls_back_to_none_when_no_job_id(self):
        """Defensive: a malformed job dict should not crash the emit."""
        from cron.scheduler import _emit_tailor_iteration_event
        emitter = self._make_emitter_with_bus()
        bad_job = {"name": "jobflow-tailor"}  # no id
        response = (
            '<TAILOR_ITERATION_JSON>'
            '{"eligible_count": 0, "tailored_count": 0, '
            '"skipped_terminal_count": 0, "skipped_other_count": 0, '
            '"reason": "no_eligible_packets"}'
            '</TAILOR_ITERATION_JSON>'
        )
        _emit_tailor_iteration_event(emitter, bad_job, response)
        assert emitter.bus.emit.call_count == 1
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["correlation_id"] is None
        assert kwargs["job_id"] is None


# ============================================================================
# Generic AGENT_ITERATION event (2026-04-30) — extends TAILOR_ITERATION
# pattern to every cron-driven agent.
# ============================================================================


class TestExtractAgentIteration:
    """Unit tests for _extract_agent_iteration — pure, no side effects."""

    def _valid_block(self, agent="scout", summary="Scanned 4 sources, 23 new") -> str:
        return (
            '<AGENT_ITERATION_JSON>'
            f'{{"agent": "{agent}", "summary": "{summary}", '
            '"counters": {"new": 23, "deduped": 11}}'
            '</AGENT_ITERATION_JSON>'
        )

    def test_happy_path_returns_parsed_payload(self):
        from cron.scheduler import _extract_agent_iteration
        text = "Some preamble.\n\n" + self._valid_block() + "\n\nDone."
        parsed, err, raw = _extract_agent_iteration(text)
        assert err is None
        assert parsed is not None
        assert parsed["agent"] == "scout"
        assert "23 new" in parsed["summary"]
        assert parsed["counters"] == {"new": 23, "deduped": 11}
        assert raw and "scout" in raw

    def test_missing_marker_returns_missing(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_REASON_MISSING,
        )
        parsed, err, raw = _extract_agent_iteration("Did some work. [SILENT]")
        assert parsed is None
        assert err == AGENT_ITERATION_REASON_MISSING
        assert raw is None

    def test_empty_input_returns_missing(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_REASON_MISSING,
        )
        for text in ("", None):
            parsed, err, _ = _extract_agent_iteration(text)
            assert err == AGENT_ITERATION_REASON_MISSING

    def test_malformed_json_returns_parse_failed(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_REASON_PARSE_FAILED,
        )
        text = "<AGENT_ITERATION_JSON>this is not json {[ </AGENT_ITERATION_JSON>"
        parsed, err, raw = _extract_agent_iteration(text)
        assert parsed is None
        assert err == AGENT_ITERATION_REASON_PARSE_FAILED
        assert raw and "not json" in raw

    def test_missing_agent_field_returns_schema_mismatch(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = '<AGENT_ITERATION_JSON>{"summary": "ok"}</AGENT_ITERATION_JSON>'
        parsed, err, _ = _extract_agent_iteration(text)
        assert err == AGENT_ITERATION_REASON_SCHEMA_MISMATCH

    def test_missing_summary_returns_schema_mismatch(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = '<AGENT_ITERATION_JSON>{"agent": "scout"}</AGENT_ITERATION_JSON>'
        parsed, err, _ = _extract_agent_iteration(text)
        assert err == AGENT_ITERATION_REASON_SCHEMA_MISMATCH

    def test_empty_string_field_returns_schema_mismatch(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = (
            '<AGENT_ITERATION_JSON>{"agent": "  ", "summary": "ok"}'
            '</AGENT_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_agent_iteration(text)
        assert err == AGENT_ITERATION_REASON_SCHEMA_MISMATCH

    def test_counters_must_be_dict(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = (
            '<AGENT_ITERATION_JSON>{"agent": "scout", "summary": "ok", '
            '"counters": [1,2,3]}</AGENT_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_agent_iteration(text)
        assert err == AGENT_ITERATION_REASON_SCHEMA_MISMATCH

    def test_counters_values_must_be_numeric_not_bool(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        # bool is subclass of int — must be rejected explicitly
        text = (
            '<AGENT_ITERATION_JSON>{"agent": "scout", "summary": "ok", '
            '"counters": {"flag": true}}</AGENT_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_agent_iteration(text)
        assert err == AGENT_ITERATION_REASON_SCHEMA_MISMATCH

    def test_anomalies_must_be_list(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = (
            '<AGENT_ITERATION_JSON>{"agent": "scout", "summary": "ok", '
            '"anomalies": "single string"}</AGENT_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_agent_iteration(text)
        assert err == AGENT_ITERATION_REASON_SCHEMA_MISMATCH

    def test_reason_must_be_string_when_present(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        text = (
            '<AGENT_ITERATION_JSON>{"agent": "scout", "summary": "ok", '
            '"reason": 123}</AGENT_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_agent_iteration(text)
        assert err == AGENT_ITERATION_REASON_SCHEMA_MISMATCH

    def test_summary_truncated_at_max_chars(self):
        from cron.scheduler import (
            _extract_agent_iteration,
            AGENT_ITERATION_SUMMARY_MAX_CHARS,
        )
        long_summary = "x" * 500
        text = (
            f'<AGENT_ITERATION_JSON>{{"agent": "scout", "summary": "{long_summary}"}}'
            '</AGENT_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_agent_iteration(text)
        assert err is None
        assert len(parsed["summary"]) == AGENT_ITERATION_SUMMARY_MAX_CHARS
        assert parsed["summary"].endswith("…")

    def test_agent_name_lowercased_and_stripped(self):
        from cron.scheduler import _extract_agent_iteration
        text = (
            '<AGENT_ITERATION_JSON>{"agent": "  Sentinel ", "summary": "ok"}'
            '</AGENT_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_agent_iteration(text)
        assert err is None
        assert parsed["agent"] == "sentinel"

    def test_optional_fields_omitted(self):
        """Minimum-viable payload (agent + summary) must succeed."""
        from cron.scheduler import _extract_agent_iteration
        text = (
            '<AGENT_ITERATION_JSON>{"agent": "scout", "summary": "ok"}'
            '</AGENT_ITERATION_JSON>'
        )
        parsed, err, _ = _extract_agent_iteration(text)
        assert err is None
        assert "counters" not in parsed
        assert "anomalies" not in parsed


class TestEmitAgentIterationEvent:
    """Behavior tests for _emit_agent_iteration_event."""

    def _emitter_with_bus(self):
        emitter = MagicMock()
        emitter.bus = MagicMock()
        return emitter

    def _scout_job(self):
        return {"id": "scout-456", "name": "jobflow-scout"}

    def test_jobflow_tailor_short_circuits_to_avoid_double_emit(self):
        """jobflow-tailor has its own dedicated TAILOR_ITERATION event;
        the generic helper must not also fire on it."""
        from cron.scheduler import _emit_agent_iteration_event
        emitter = self._emitter_with_bus()
        tailor_job = {"id": "x", "name": "jobflow-tailor"}
        valid = (
            '<AGENT_ITERATION_JSON>{"agent": "tailor", "summary": "ok"}'
            '</AGENT_ITERATION_JSON>'
        )
        _emit_agent_iteration_event(emitter, tailor_job, valid)
        assert emitter.bus.emit.call_count == 0

    def test_no_emitter_short_circuits(self):
        from cron.scheduler import _emit_agent_iteration_event
        # Should not raise
        _emit_agent_iteration_event(None, self._scout_job(), "")

    def test_unknown_job_missing_marker_is_silent(self):
        """Non-canonical / ad-hoc jobs that omit the marker emit nothing —
        we only synthesize for known canonical agents (see fallback below).
        This preserves the original opt-in semantics for legacy/ad-hoc crons
        that never opted into AGENT_ITERATION."""
        from cron.scheduler import _emit_agent_iteration_event
        emitter = self._emitter_with_bus()
        adhoc_job = {"id": "adhoc-1", "name": "ad-hoc-cron-foo"}
        _emit_agent_iteration_event(emitter, adhoc_job, "Done.")
        assert emitter.bus.emit.call_count == 0

    def test_happy_path_emits_agent_iteration(self):
        from cron.scheduler import _emit_agent_iteration_event
        from events.schema import EventType
        emitter = self._emitter_with_bus()
        response = (
            '<AGENT_ITERATION_JSON>'
            '{"agent": "scout", "summary": "Scanned 4 sources, 23 new", '
            '"counters": {"new": 23, "deduped": 11}}'
            '</AGENT_ITERATION_JSON>'
        )
        _emit_agent_iteration_event(emitter, self._scout_job(), response)
        assert emitter.bus.emit.call_count == 1
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.AGENT_ITERATION
        assert kwargs["source"] == "scout"
        assert kwargs["correlation_id"] == "scout-456"
        payload = kwargs["payload"]
        assert payload["agent"] == "scout"
        assert "23 new" in payload["summary"]
        assert payload["counters"]["new"] == 23
        assert payload["job_name"] == "jobflow-scout"
        assert payload["job_id"] == "scout-456"

    def test_malformed_marker_emits_agent_error(self):
        from cron.scheduler import (
            _emit_agent_iteration_event,
            AGENT_ITERATION_REASON_PARSE_FAILED,
        )
        from events.schema import EventType
        emitter = self._emitter_with_bus()
        response = (
            "<AGENT_ITERATION_JSON>this is not json [{ </AGENT_ITERATION_JSON>"
        )
        _emit_agent_iteration_event(emitter, self._scout_job(), response)
        assert emitter.bus.emit.call_count == 1
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.AGENT_ERROR
        assert kwargs["payload"]["reason"] == AGENT_ITERATION_REASON_PARSE_FAILED
        assert kwargs["payload"]["job_name"] == "jobflow-scout"
        assert "not json" in kwargs["payload"]["detail"]

    def test_schema_mismatch_emits_agent_error(self):
        from cron.scheduler import (
            _emit_agent_iteration_event,
            AGENT_ITERATION_REASON_SCHEMA_MISMATCH,
        )
        from events.schema import EventType
        emitter = self._emitter_with_bus()
        # Missing both required fields
        response = '<AGENT_ITERATION_JSON>{"foo": "bar"}</AGENT_ITERATION_JSON>'
        _emit_agent_iteration_event(emitter, self._scout_job(), response)
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.AGENT_ERROR
        assert kwargs["payload"]["reason"] == AGENT_ITERATION_REASON_SCHEMA_MISMATCH

    def test_emit_failure_does_not_propagate(self):
        from cron.scheduler import _emit_agent_iteration_event
        emitter = MagicMock()
        emitter.bus = MagicMock()
        emitter.bus.emit.side_effect = RuntimeError("simulated DB lock")
        # Must not raise
        _emit_agent_iteration_event(emitter, self._scout_job(), "anything")

    # ------------------------------------------------------------------
    # Canonical agent-name override (2026-04-30) — payload.agent is keyed
    # off job_name via canonical_agent_source, not LLM choice.  See
    # docs/superpowers/specs/2026-04-30-agent-iteration-canonical-name.md
    # ------------------------------------------------------------------

    def test_devflow_bridge_overrides_llm_supplied_agent(self):
        """devflow-bridge job names emit ~5 different LLM-chosen agent
        names (devflow, hermes_to_devflow, bridge, watchdog, …).  The
        canonical mapping forces them all to 'devflow' so Telegram routing
        is deterministic."""
        from cron.scheduler import _emit_agent_iteration_event
        from events.schema import EventType
        emitter = self._emitter_with_bus()
        bridge_job = {"id": "bridge-1", "name": "devflow-bridge"}
        response = (
            '<AGENT_ITERATION_JSON>'
            '{"agent": "watchdog", "summary": "saw a successful run"}'
            '</AGENT_ITERATION_JSON>'
        )
        _emit_agent_iteration_event(emitter, bridge_job, response)
        assert emitter.bus.emit.call_count == 1
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.AGENT_ITERATION
        payload = kwargs["payload"]
        assert payload["agent"] == "devflow"
        assert payload["agent_llm_supplied"] == "watchdog"
        assert kwargs["source"] == "devflow"

    def test_jobflow_devflow_overrides_llm_supplied_agent(self):
        """jobflow-devflow's prompt says 'Act as the DevFlow agent for
        JobFlow on Hermes' — the LLM may pick devflow|jobflow|jaum.  The
        canonical mapping forces all three to 'devflow' so the Friday
        standup lands in devflow_firehose every time."""
        from cron.scheduler import _emit_agent_iteration_event
        emitter = self._emitter_with_bus()
        standup_job = {"id": "standup-1", "name": "jobflow-devflow"}
        response = (
            '<AGENT_ITERATION_JSON>'
            '{"agent": "jobflow", "summary": "Weekday standup digest"}'
            '</AGENT_ITERATION_JSON>'
        )
        _emit_agent_iteration_event(emitter, standup_job, response)
        kwargs = emitter.bus.emit.call_args.kwargs
        payload = kwargs["payload"]
        assert payload["agent"] == "devflow"
        assert payload["agent_llm_supplied"] == "jobflow"
        assert kwargs["source"] == "devflow"

    def test_unknown_job_keeps_llm_supplied_agent(self):
        """If job_name has no canonical mapping (ad-hoc cron, mistyped
        name), don't break routing by overriding to a non-canonical
        string.  Fall back to LLM-supplied so existing/legacy jobs keep
        working."""
        from cron.scheduler import _emit_agent_iteration_event
        emitter = self._emitter_with_bus()
        adhoc_job = {"id": "adhoc-1", "name": "ad-hoc-cron-foo"}
        response = (
            '<AGENT_ITERATION_JSON>'
            '{"agent": "watchdog", "summary": "did a thing"}'
            '</AGENT_ITERATION_JSON>'
        )
        _emit_agent_iteration_event(emitter, adhoc_job, response)
        kwargs = emitter.bus.emit.call_args.kwargs
        payload = kwargs["payload"]
        assert payload["agent"] == "watchdog"
        assert "agent_llm_supplied" not in payload
        assert kwargs["source"] == "watchdog"

    def test_canonical_override_is_idempotent_when_llm_already_correct(self):
        """When the LLM happens to pick the canonical name already, the
        override is a no-op semantically.  agent_llm_supplied still
        records what the LLM said (for audit consistency)."""
        from cron.scheduler import _emit_agent_iteration_event
        emitter = self._emitter_with_bus()
        bridge_job = {"id": "bridge-2", "name": "devflow-bridge"}
        response = (
            '<AGENT_ITERATION_JSON>'
            '{"agent": "devflow", "summary": "all good"}'
            '</AGENT_ITERATION_JSON>'
        )
        _emit_agent_iteration_event(emitter, bridge_job, response)
        kwargs = emitter.bus.emit.call_args.kwargs
        payload = kwargs["payload"]
        assert payload["agent"] == "devflow"
        assert payload["agent_llm_supplied"] == "devflow"

    # ------------------------------------------------------------------
    # Deterministic marker-missing fallback (2026-04-30) — when a known
    # canonical agent's job omits the AGENT_ITERATION_JSON marker, we
    # synthesize a placeholder event so 100% of canonical-agent runs
    # produce an AGENT_ITERATION trail.  Non-canonical jobs still
    # silently no-op so we don't spam ad-hoc crons.  See
    # docs/superpowers/specs/2026-04-30-agent-iteration-marker-fallback.md
    # ------------------------------------------------------------------

    def test_devflow_bridge_missing_marker_synthesizes_event(self):
        """devflow-bridge intermittently (~46% of runs) omits the
        AGENT_ITERATION_JSON marker entirely.  The fallback synthesizes
        a stand-in event so devflow_firehose still gets a heartbeat."""
        from cron.scheduler import _emit_agent_iteration_event
        from events.schema import EventType
        emitter = self._emitter_with_bus()
        bridge_job = {"id": "bridge-77", "name": "devflow-bridge"}
        _emit_agent_iteration_event(emitter, bridge_job, "Done with no marker.")
        assert emitter.bus.emit.call_count == 1
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.AGENT_ITERATION
        assert kwargs["source"] == "devflow"
        assert kwargs["correlation_id"] == "bridge-77"
        payload = kwargs["payload"]
        assert payload["agent"] == "devflow"
        assert payload["synthesized"] is True
        assert "synthesized" in payload["summary"]
        assert "AGENT_ITERATION_JSON" in payload["summary"]
        assert payload["summary"].startswith("devflow")
        assert payload["job_name"] == "devflow-bridge"
        assert payload["job_id"] == "bridge-77"
        # No LLM was consulted, so no agent_llm_supplied field.
        assert "agent_llm_supplied" not in payload

    def test_jobflow_applier_missing_marker_synthesizes_event(self):
        """jobflow-applier maps to canonical 'applier' — fallback fires
        for any canonical agent, not just devflow."""
        from cron.scheduler import _emit_agent_iteration_event
        from events.schema import EventType
        emitter = self._emitter_with_bus()
        applier_job = {"id": "applier-9", "name": "jobflow-applier"}
        _emit_agent_iteration_event(emitter, applier_job, "")
        assert emitter.bus.emit.call_count == 1
        kwargs = emitter.bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.AGENT_ITERATION
        assert kwargs["source"] == "applier"
        payload = kwargs["payload"]
        assert payload["agent"] == "applier"
        assert payload["synthesized"] is True
        assert payload["summary"].startswith("applier")
        assert payload["job_name"] == "jobflow-applier"
        assert payload["job_id"] == "applier-9"

    def test_jobflow_tailor_missing_marker_stays_silent(self):
        """jobflow-tailor short-circuits before extraction (its dedicated
        TAILOR_ITERATION emit hook handles its lifecycle).  The fallback
        must NOT synthesize a generic AGENT_ITERATION for tailor — that
        would double-emit alongside the tailor-specific event."""
        from cron.scheduler import _emit_agent_iteration_event
        emitter = self._emitter_with_bus()
        tailor_job = {"id": "tailor-1", "name": "jobflow-tailor"}
        _emit_agent_iteration_event(emitter, tailor_job, "Done with no marker.")
        assert emitter.bus.emit.call_count == 0


# Cron same-job concurrency guard -- added 2026-04-30. Closes the
# 2026-04-30 sentinel-vip-morning triple-fire (canonical case
# event_id 4edcb4b1-aa07-4dbb-b799-8af167d4f92e). The third fire
# at 14:49 hung in Anthropic and was killed only when the gateway
# restarted at 14:56. Two concurrent sentinel fires would also
# collide on the browser-harness per-process lock.
@pytest.mark.usefixtures("_tick_lock_isolated")
class TestDuplicateFireGuard:
    @pytest.fixture(autouse=True)
    def _reset_in_flight(self):
        """Clear _in_flight before AND after each test to keep tests independent."""
        from cron import scheduler as sch
        sch._in_flight.clear()
        yield
        sch._in_flight.clear()

    def _job(self, job_id="092f4ed7657c", name="sentinel-vip-morning"):
        return {"id": job_id, "name": name, "deliver": "local"}

    def test_concurrent_duplicate_fire_emits_skip_and_blocks_second_run_job(self):
        """Canonical 2026-04-30 sentinel triple-fire scenario.

        Two _process_job calls land for the same job_id; the first wins the
        in-flight slot and proceeds, the second emits CRON_SKIPPED_DUPLICATE
        and never reaches run_job (which is what would have collided on the
        Chrome browser-harness lock).
        """
        import threading
        import time
        from events.schema import EventType

        release = threading.Event()
        run_job_call_count = 0
        run_job_lock = threading.Lock()

        def blocking_run_job(job):
            nonlocal run_job_call_count
            with run_job_lock:
                run_job_call_count += 1
            # Hold the slot long enough that the parallel second fire's
            # _process_job is guaranteed to have crossed the guard.
            release.wait(timeout=5)
            return (True, "# output", "response", None)

        emitter = MagicMock()
        # Give on_job_started a deterministic event_id so we can assert it
        # propagates into prior_cron_started_event_id on the skip event.
        emitter.on_job_started.return_value = "4edcb4b1-aa07-4dbb-b799-8af167d4f92e"

        skip_calls = []

        def capture_skip(**kwargs):
            skip_calls.append(kwargs)
            return "skip-evt-id"
        emitter.on_job_skipped_duplicate.side_effect = capture_skip

        # Schedule release once both fires have had a chance to enter
        # _process_job. The second fire returns False quickly via the
        # guard; the first is then released to complete normally.
        def release_after_delay():
            time.sleep(0.4)
            release.set()
        threading.Thread(target=release_after_delay, daemon=True).start()

        # Two due-job entries with the SAME job_id reproduce the
        # trigger_job race observed in production (mirroring trigger_job
        # firing twice within one tick window).
        job = self._job()

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([dict(job), dict(job)], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job", side_effect=blocking_run_job), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        # Exactly one fire reached run_job -- the second was blocked.
        assert run_job_call_count == 1, (
            f"Expected exactly one run_job invocation; got {run_job_call_count}"
        )
        # Exactly one cron_started -- the duplicate must NOT have emitted one.
        assert emitter.on_job_started.call_count == 1, (
            f"on_job_started called {emitter.on_job_started.call_count}x; "
            "duplicate fire emitted cron_started anyway"
        )
        # Exactly one CRON_SKIPPED_DUPLICATE.
        assert len(skip_calls) == 1, f"skip_calls={skip_calls!r}"

        skip = skip_calls[0]
        assert skip["job_id"] == "092f4ed7657c"
        assert skip["job_name"] == "sentinel-vip-morning"
        assert skip["reason"] == "concurrent_fire_blocked"
        # The skip event references the live in-flight cron_started.
        assert (
            skip["prior_cron_started_event_id"]
            == "4edcb4b1-aa07-4dbb-b799-8af167d4f92e"
        )
        assert skip["prior_elapsed_seconds"] >= 0
        # Sanity: the EventType identifier is also stable.
        assert EventType.CRON_SKIPPED_DUPLICATE.type_string == "cron_skipped_duplicate"

    def test_in_flight_cleared_after_successful_run(self):
        """After a job completes, the same job_id must be allowed to fire again."""
        from cron import scheduler as sch

        emitter = MagicMock()
        emitter.on_job_started.return_value = "evt-1"

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._job()], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job",
                   return_value=(True, "# output", "response", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        assert "092f4ed7657c" not in sch._in_flight, (
            "_in_flight must be cleared after a successful run; "
            f"still holds {sch._in_flight!r}"
        )

    def test_in_flight_cleared_after_run_job_exception(self):
        """An exception inside run_job must still release the in-flight slot."""
        from cron import scheduler as sch

        emitter = MagicMock()
        emitter.on_job_started.return_value = "evt-1"

        def boom(job):
            raise RuntimeError("simulated agent crash")

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._job()], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job", side_effect=boom), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        assert "092f4ed7657c" not in sch._in_flight, (
            "_in_flight must be cleared even when run_job raises"
        )

    def test_different_job_ids_do_not_collide(self):
        """The guard is keyed by job_id; unrelated jobs run in parallel."""
        import threading

        emitter = MagicMock()
        emitter.on_job_started.side_effect = ["evt-a", "evt-b"]

        skip_calls = []
        emitter.on_job_skipped_duplicate.side_effect = (
            lambda **kw: skip_calls.append(kw)
        )

        run_count = 0
        run_lock = threading.Lock()

        def mock_run(job):
            nonlocal run_count
            with run_lock:
                run_count += 1
            return (True, "# output", "response", None)

        jobs = [
            {"id": "job-a", "name": "a", "deliver": "local"},
            {"id": "job-b", "name": "b", "deliver": "local"},
        ]

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=(jobs, [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job", side_effect=mock_run), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "job-a", "consecutive_errors": 0},
                 {"id": "job-b", "consecutive_errors": 0},
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        assert run_count == 2
        assert emitter.on_job_started.call_count == 2
        assert skip_calls == [], (
            "Distinct job_ids must not be flagged as duplicates; "
            f"got skip_calls={skip_calls!r}"
        )

    def test_prior_fire_exceeded_timeout_reason(self, monkeypatch):
        """When the prior fire has been registered for longer than the
        configured hard-timeout, the reason switches to
        'prior_fire_exceeded_timeout' and the new fire is still rejected."""
        import time
        from cron import scheduler as sch

        # Force the guard's effective timeout down to 1 second so we can
        # seed an "old" in-flight record without sleeping past the
        # production default.
        monkeypatch.setenv("HERMES_CRON_HARD_TIMEOUT", "1")

        # Seed an in-flight record that pre-dates the timeout window.
        sch._in_flight["092f4ed7657c"] = sch._InFlightRecord(
            start_monotonic=time.monotonic() - 9999.0,
            job_name="sentinel-vip-morning",
            cron_started_event_id="prior-evt-id",
        )

        emitter = MagicMock()
        skip_calls = []
        emitter.on_job_skipped_duplicate.side_effect = (
            lambda **kw: skip_calls.append(kw)
        )

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([self._job()], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job") as mock_run, \
             patch("cron.scheduler.save_job_output"), \
             patch("cron.scheduler._deliver_result"), \
             patch("cron.scheduler.mark_job_run"):
            from cron.scheduler import tick
            tick(verbose=False)

        # run_job NEVER invoked (declined to start on top of wedged prior).
        mock_run.assert_not_called()
        # cron_started NOT emitted for this fire.
        assert emitter.on_job_started.call_count == 0

        assert len(skip_calls) == 1
        skip = skip_calls[0]
        assert skip["reason"] == "prior_fire_exceeded_timeout"
        assert skip["prior_cron_started_event_id"] == "prior-evt-id"
        assert skip["prior_elapsed_seconds"] >= 9999.0


class TestInFlightRegistryShape:
    """Document the _in_flight registry contract for Guard #1 (cron_aborted
    on shutdown), which builds on the same registry."""

    def test_registry_is_module_level_dict(self):
        """_in_flight is a module-level dict keyed by job_id."""
        from cron import scheduler as sch
        assert isinstance(sch._in_flight, dict)

    def test_record_has_documented_fields(self):
        from cron.scheduler import _InFlightRecord
        rec = _InFlightRecord(
            start_monotonic=42.0,
            job_name="any",
            cron_started_event_id=None,
        )
        assert rec.start_monotonic == 42.0
        assert rec.job_name == "any"
        assert rec.cron_started_event_id is None

    def test_lock_is_module_level(self):
        """_in_flight_lock serializes registry mutations across threads."""
        import threading
        from cron import scheduler as sch
        # threading.Lock is a factory; the underlying type is exposed via
        # type(threading.Lock()) — this checks we have a real lock object.
        assert isinstance(sch._in_flight_lock, type(threading.Lock()))


# Min-seconds-between-fires guard (Guard #4, 2026-04-30 follow-up) ---------
# Closes the SEQUENTIAL-burst gap left by Guard #3.  The 2026-04-30 sentinel
# triple-fire (14:02 / 14:34 / 14:49 UTC) had each prior fire fully
# completing before the next arrived -- so the in-flight registry was empty
# at each new fire's arrival and Guard #3 never engaged.  See
# sentinel-vip-burst-rc-2026-04-30.md §6 for the full design.
@pytest.mark.usefixtures("_tick_lock_isolated")
class TestMinIntervalGuard:
    @pytest.fixture(autouse=True)
    def _reset_in_flight_and_env(self, monkeypatch):
        """Clear _in_flight and the env-var override before/after each test."""
        from cron import scheduler as sch
        sch._in_flight.clear()
        monkeypatch.delenv("HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES", raising=False)
        yield
        sch._in_flight.clear()

    def _job(
        self,
        job_id="092f4ed7657c",
        name="sentinel-vip-morning",
        min_seconds_between_fires=None,
        last_run_at=None,
    ):
        from hermes_time import now as _now
        # Default last_run_at = recent (5 min ago), default min=1800 (30 min)
        if last_run_at is None:
            last_run_at = (_now()).isoformat()
        job = {"id": job_id, "name": name, "deliver": "local"}
        if min_seconds_between_fires is not None:
            job["min_seconds_between_fires"] = min_seconds_between_fires
        if last_run_at is not None:
            job["last_run_at"] = last_run_at
        return job

    def test_fire_blocked_when_within_min_interval(self):
        """last_run_at = now - 5 min, min = 30 min => fire is blocked,
        run_job NOT called, cron_skipped_min_interval is emitted."""
        from datetime import timedelta
        from events.schema import EventType
        from hermes_time import now as _now

        five_min_ago = (_now() - timedelta(minutes=5)).isoformat()
        job = self._job(
            min_seconds_between_fires=1800,
            last_run_at=five_min_ago,
        )

        emitter = MagicMock()
        emitter.on_job_started.return_value = "should-not-be-emitted"
        skip_calls = []

        def capture_skip(**kwargs):
            skip_calls.append(kwargs)
            return "skip-evt-id"
        emitter.on_job_skipped_min_interval.side_effect = capture_skip

        run_job_called = False

        def fail_run_job(j):
            nonlocal run_job_called
            run_job_called = True
            return (True, "# output", "response", None)

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([job], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job", side_effect=fail_run_job), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        # Guard #4 must reject before run_job is invoked.
        assert run_job_called is False, "run_job should NOT be called"
        # cron_started must NOT have been emitted -- the guard fires earlier.
        assert emitter.on_job_started.call_count == 0, (
            "on_job_started should NOT have been emitted; Guard #4 rejects "
            "before the cron_started emit point"
        )
        # Exactly one cron_skipped_min_interval emitted.
        assert len(skip_calls) == 1, f"skip_calls={skip_calls!r}"
        skip = skip_calls[0]
        assert skip["job_id"] == "092f4ed7657c"
        assert skip["job_name"] == "sentinel-vip-morning"
        assert skip["last_run_at"] == five_min_ago
        assert skip["min_seconds_between_fires"] == 1800
        # ~300s (5 min) elapsed; allow some tolerance for test runtime.
        assert 290 <= skip["elapsed_since_last_seconds"] <= 320, (
            f"elapsed_since_last_seconds out of expected range: "
            f"{skip['elapsed_since_last_seconds']}"
        )
        # Sanity: the EventType identifier is stable.
        assert (
            EventType.CRON_SKIPPED_MIN_INTERVAL.type_string
            == "cron_skipped_min_interval"
        )

    def test_fire_proceeds_when_past_min_interval(self):
        """last_run_at = now - 31 min, min = 30 min => fire proceeds normally.

        Boundary: even one minute past the threshold lets the fire through.
        """
        from datetime import timedelta
        from hermes_time import now as _now

        thirty_one_min_ago = (_now() - timedelta(minutes=31)).isoformat()
        job = self._job(
            min_seconds_between_fires=1800,
            last_run_at=thirty_one_min_ago,
        )

        emitter = MagicMock()
        emitter.on_job_started.return_value = "evt-1"

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([job], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job",
                   return_value=(True, "# output", "response", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        # cron_started SHOULD have been emitted -- guard didn't engage.
        assert emitter.on_job_started.call_count == 1
        # No skip event emitted.
        assert emitter.on_job_skipped_min_interval.call_count == 0

    def test_guard_off_by_default(self):
        """No min_seconds_between_fires field, no env var => fire proceeds
        even with last_run_at very recent.  Backward-compat default."""
        from datetime import timedelta
        from hermes_time import now as _now

        ten_seconds_ago = (_now() - timedelta(seconds=10)).isoformat()
        job = self._job(
            min_seconds_between_fires=None,  # explicitly off
            last_run_at=ten_seconds_ago,
        )
        # Field is omitted from the dict so it's truly absent
        job.pop("min_seconds_between_fires", None)

        emitter = MagicMock()
        emitter.on_job_started.return_value = "evt-1"

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([job], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job",
                   return_value=(True, "# output", "response", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        assert emitter.on_job_started.call_count == 1
        assert emitter.on_job_skipped_min_interval.call_count == 0

    def test_env_var_default_applies_when_per_job_unset(self, monkeypatch):
        """When per-job field is unset but HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES
        is set, the env-var value applies."""
        from datetime import timedelta
        from hermes_time import now as _now

        monkeypatch.setenv("HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES", "1800")

        five_min_ago = (_now() - timedelta(minutes=5)).isoformat()
        job = self._job(
            min_seconds_between_fires=None,
            last_run_at=five_min_ago,
        )
        job.pop("min_seconds_between_fires", None)

        emitter = MagicMock()
        emitter.on_job_started.return_value = "evt-should-not-fire"
        skip_calls = []
        emitter.on_job_skipped_min_interval.side_effect = (
            lambda **kw: (skip_calls.append(kw), "skip-evt")[1]
        )

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([job], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job",
                   return_value=(True, "# output", "response", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        assert len(skip_calls) == 1
        assert skip_calls[0]["min_seconds_between_fires"] == 1800

    def test_per_job_zero_overrides_env_var(self, monkeypatch):
        """Per-job ``min_seconds_between_fires=0`` opts OUT of the guard
        even when the env-var default is non-zero.  Per-job value wins."""
        from datetime import timedelta
        from hermes_time import now as _now

        monkeypatch.setenv("HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES", "1800")

        thirty_seconds_ago = (_now() - timedelta(seconds=30)).isoformat()
        job = self._job(
            min_seconds_between_fires=0,
            last_run_at=thirty_seconds_ago,
        )

        emitter = MagicMock()
        emitter.on_job_started.return_value = "evt-1"

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([job], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job",
                   return_value=(True, "# output", "response", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        assert emitter.on_job_started.call_count == 1
        assert emitter.on_job_skipped_min_interval.call_count == 0

    def test_in_flight_slot_released_on_reject(self):
        """After Guard #4 rejects, _in_flight must be empty so the next
        legitimate fire is not blocked by the rejected one's still-held slot."""
        from datetime import timedelta
        from cron import scheduler as sch
        from hermes_time import now as _now

        five_min_ago = (_now() - timedelta(minutes=5)).isoformat()
        job = self._job(
            min_seconds_between_fires=1800,
            last_run_at=five_min_ago,
        )

        emitter = MagicMock()
        emitter.on_job_started.return_value = "evt-not-emitted"

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([job], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job",
                   return_value=(True, "# output", "response", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        assert "092f4ed7657c" not in sch._in_flight, (
            "_in_flight must be cleared after Guard #4 rejects; "
            f"still holds {sch._in_flight!r}"
        )

    def test_malformed_last_run_at_does_not_block(self):
        """A garbage last_run_at string must not wedge the scheduler.
        Defensive: production data should never land here, but a parse
        bug must let the job proceed."""
        job = self._job(
            min_seconds_between_fires=1800,
            last_run_at="not-an-iso-timestamp",
        )

        emitter = MagicMock()
        emitter.on_job_started.return_value = "evt-1"

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([job], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job",
                   return_value=(True, "# output", "response", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        # Fire proceeds; no skip event.
        assert emitter.on_job_started.call_count == 1
        assert emitter.on_job_skipped_min_interval.call_count == 0

    def test_no_last_run_at_does_not_block(self):
        """A first-ever fire (last_run_at absent) must not be blocked by
        Guard #4.  This is the new-job bootstrap case."""
        job = self._job(
            min_seconds_between_fires=1800,
            last_run_at=None,
        )
        # Explicitly absent
        job.pop("last_run_at", None)

        emitter = MagicMock()
        emitter.on_job_started.return_value = "evt-1"

        with patch("cron.scheduler.get_due_and_skipped_jobs", return_value=([job], [])), \
             patch("cron.scheduler.advance_next_run"), \
             patch("cron.scheduler._get_event_emitter", return_value=emitter), \
             patch("cron.scheduler.run_job",
                   return_value=(True, "# output", "response", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run"), \
             patch("cron.jobs.load_jobs", return_value=[
                 {"id": "092f4ed7657c", "consecutive_errors": 0}
             ]):
            from cron.scheduler import tick
            tick(verbose=False)

        assert emitter.on_job_started.call_count == 1
        assert emitter.on_job_skipped_min_interval.call_count == 0

    def test_helper_resolves_per_job_then_env_then_default(self, monkeypatch):
        """_job_min_seconds_between_fires resolution priority."""
        from cron.scheduler import _job_min_seconds_between_fires

        # 1. Per-job field wins
        monkeypatch.setenv("HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES", "9999")
        assert _job_min_seconds_between_fires(
            {"min_seconds_between_fires": 30}
        ) == 30
        # Per-job 0 wins (opt-out)
        assert _job_min_seconds_between_fires(
            {"min_seconds_between_fires": 0}
        ) == 0

        # 2. Env var wins when per-job unset
        assert _job_min_seconds_between_fires({}) == 9999

        # 3. Default 0 when both unset
        monkeypatch.delenv("HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES")
        assert _job_min_seconds_between_fires({}) == 0

        # 4. Garbage in env var falls back to default 0
        monkeypatch.setenv("HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES", "not-an-int")
        assert _job_min_seconds_between_fires({}) == 0

        # 5. Garbage in per-job field falls back to env var
        monkeypatch.setenv("HERMES_CRON_MIN_SECONDS_BETWEEN_FIRES", "1800")
        assert _job_min_seconds_between_fires(
            {"min_seconds_between_fires": "not-an-int"}
        ) == 1800

        # 6. Negative per-job value clamped to 0
        assert _job_min_seconds_between_fires(
            {"min_seconds_between_fires": -100}
        ) == 0


class TestPerJobSoftDeadline:
    """Per-job soft deadline (2026-06-10 audit M1 T1.4).

    A hung LLM job used to block its worker forever — and in the
    sequential bucket, every later job in the tick. The deadline wrapper
    bounds the WAIT (threads can't be killed): parallel-safe jobs are
    marked failed + slot-released + abandoned; sequential jobs alert and
    keep waiting (abandoning them would race os.environ restore).
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from cron import scheduler as sched
        yield
        with sched._in_flight_lock:
            sched._in_flight.clear()

    def _stub_emitter(self, monkeypatch):
        from cron import scheduler as sched
        calls = []

        class _Emitter:
            def on_job_completed(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(sched, "_get_event_emitter", lambda: _Emitter())
        return calls

    def test_fast_job_returns_result_unchanged(self, monkeypatch):
        import contextvars
        from cron import scheduler as sched
        calls = self._stub_emitter(monkeypatch)

        def fn(job, _abandoned=None):
            return True

        job = {"id": "fast-job", "name": "fast", "timeout_seconds": 5}
        assert sched._run_callable_with_deadline(
            job, fn, True, contextvars.copy_context()
        ) is True
        assert calls == []

    def test_zero_timeout_disables_deadline(self, monkeypatch):
        import contextvars
        from cron import scheduler as sched
        self._stub_emitter(monkeypatch)
        seen = []

        def fn(job, _abandoned="not-passed"):
            seen.append(_abandoned)
            return True

        job = {"id": "off-job", "name": "off", "timeout_seconds": 0}
        assert sched._run_callable_with_deadline(
            job, fn, True, contextvars.copy_context()
        ) is True
        # Disabled path runs process_fn directly, no abandon plumbing.
        assert seen == ["not-passed"]

    def test_parallel_timeout_marks_releases_and_abandons(self, monkeypatch):
        import contextvars
        import threading
        import time as _t
        from cron import scheduler as sched

        calls = self._stub_emitter(monkeypatch)
        marked = []
        monkeypatch.setattr(
            sched, "mark_job_run",
            lambda job_id, success, error, **kw: marked.append((job_id, success, error)),
        )
        runaway_done = threading.Event()
        saw_abandoned = []

        def fn(job, _abandoned=None):
            _t.sleep(1.2)  # well past the 0.2s deadline
            saw_abandoned.append(_abandoned.is_set())
            runaway_done.set()
            return True

        job = {"id": "hung-job", "name": "hung", "timeout_seconds": 0.2}
        # Simulate the worker's Guard #3 registration.
        assert sched._try_register_in_flight("hung-job", "hung") is None

        t0 = _t.monotonic()
        result = sched._run_callable_with_deadline(
            job, fn, True, contextvars.copy_context()
        )
        elapsed = _t.monotonic() - t0

        assert result is False
        assert elapsed < 1.0, "must return at the deadline, not at completion"
        assert marked == [("hung-job", False, marked[0][2])]
        assert "soft deadline exceeded" in marked[0][2]
        assert len(calls) == 1 and calls[0]["success"] is False
        # Slot released at the deadline so the next fire can register…
        assert sched._try_register_in_flight("hung-job", "hung") is None
        # …and the runaway finishing later must NOT pop the new record.
        assert runaway_done.wait(3), "runaway worker never finished"
        _t.sleep(0.1)
        with sched._in_flight_lock:
            assert "hung-job" in sched._in_flight
        assert saw_abandoned == [True]

    def test_sequential_timeout_alerts_but_waits_for_completion(self, monkeypatch):
        import contextvars
        import time as _t
        from cron import scheduler as sched

        calls = self._stub_emitter(monkeypatch)
        marked = []
        monkeypatch.setattr(
            sched, "mark_job_run",
            lambda *a, **kw: marked.append(a),
        )

        def fn(job, _abandoned=None):
            _t.sleep(0.7)
            return True

        job = {"id": "seq-job", "name": "seq", "timeout_seconds": 0.2}
        t0 = _t.monotonic()
        result = sched._run_callable_with_deadline(
            job, fn, False, contextvars.copy_context()
        )
        elapsed = _t.monotonic() - t0

        assert result is True, "sequential path must wait for the real result"
        assert elapsed >= 0.65
        # Alert fired at the deadline…
        assert len(calls) == 1 and calls[0]["success"] is False
        assert "sequential job" in calls[0]["error"]
        # …but the run was NOT marked failed (it's still owned by _process_job).
        assert marked == []

    def test_release_started_before_protects_successor_record(self):
        import time as _t
        from cron import scheduler as sched

        assert sched._try_register_in_flight("guard-job", "guard") is None
        cutoff = _t.monotonic()
        _t.sleep(0.01)
        # Successor registers after the cutoff (deadline handler's view).
        sched._release_in_flight("guard-job")
        assert sched._try_register_in_flight("guard-job", "guard") is None
        sched._release_in_flight_started_before("guard-job", cutoff)
        with sched._in_flight_lock:
            assert "guard-job" in sched._in_flight, (
                "a record started after the cutoff must survive"
            )

    def test_per_job_script_timeout_overrides_global(self):
        import time as _t
        from cron import scheduler as sched

        sdir = sched._get_hermes_home() / "scripts"
        sdir.mkdir(parents=True, exist_ok=True)
        slow = sdir / "slow_gate_probe.py"
        slow.write_text(
            "import time\ntime.sleep(8)\nprint('done')\n", encoding="utf-8"
        )
        t0 = _t.monotonic()
        ok, out = sched._run_job_script("slow_gate_probe.py", timeout_s=1)
        elapsed = _t.monotonic() - t0
        assert ok is False
        assert "timed out after 1s" in out
        assert elapsed < 6, "per-job timeout must override the 120s global"
        # Invalid / non-positive overrides fall back to the global default
        # (probe a fast script so the global never actually elapses).
        fast = sdir / "fast_gate_probe.py"
        fast.write_text("print('ok')\n", encoding="utf-8")
        ok2, out2 = sched._run_job_script("fast_gate_probe.py", timeout_s="bogus")
        assert ok2 is True and out2 == "ok"

    def test_timeout_resolution_priority(self, monkeypatch):
        from cron import scheduler as sched

        monkeypatch.setenv("HERMES_CRON_JOB_TIMEOUT_SECONDS", "111")
        assert sched._job_timeout_seconds({"timeout_seconds": 42}) == 42.0
        assert sched._job_timeout_seconds({}) == 111.0
        monkeypatch.delenv("HERMES_CRON_JOB_TIMEOUT_SECONDS")
        monkeypatch.setattr(
            sched, "load_config",
            lambda: {"cron": {"job_timeout_seconds": 77}},
        )
        assert sched._job_timeout_seconds({}) == 77.0
        monkeypatch.setattr(sched, "load_config", lambda: {})
        assert sched._job_timeout_seconds({}) == sched._DEFAULT_JOB_TIMEOUT_S
        # Per-job <= 0 disables.
        assert sched._job_timeout_seconds({"timeout_seconds": 0}) == 0.0
