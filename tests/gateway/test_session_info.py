"""Tests for GatewayRunner._format_session_info — session config surfacing."""

import pytest
from unittest.mock import patch

from gateway.run import GatewayRunner


@pytest.fixture()
def runner():
    """Create a bare GatewayRunner without __init__."""
    return GatewayRunner.__new__(GatewayRunner)


def _patch_info(tmp_path, config_yaml, model, runtime):
    """Return a context-manager stack that patches _format_session_info deps."""
    cfg_path = tmp_path / "config.yaml"
    if config_yaml is not None:
        cfg_path.write_text(config_yaml)
    return (
        patch("gateway.run._hermes_home", tmp_path),
        patch("gateway.run._resolve_gateway_model", return_value=model),
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value=runtime),
    )


class TestFormatSessionInfo:

    def test_includes_model_name(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: anthropic/claude-opus-4.6\n  provider: openrouter\n",
                                  "anthropic/claude-opus-4.6",
                                  {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "k"})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "claude-opus-4.6" in info


    def test_config_context_length(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  context_length: 32768\n",
                                  "test-model",
                                  {"provider": "custom", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "32K" in info
        assert "config" in info

    def test_default_fallback_hint(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: unknown-model-xyz\n",
                                  "unknown-model-xyz",
                                  {"provider": "", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "256K" in info
        assert "model.context_length" in info

    def test_local_endpoint_shown(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(
            tmp_path,
            "model:\n  default: qwen3:8b\n  provider: custom\n  base_url: http://localhost:11434/v1\n  context_length: 8192\n",
            "qwen3:8b",
            {"provider": "custom", "base_url": "http://localhost:11434/v1", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "localhost:11434" in info
        assert "8K" in info

    def test_arabic_session_info_labels(self, runner, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ar")
        p1, p2, p3 = _patch_info(
            tmp_path,
            "model:\n  default: test-model\n  provider: custom\n  context_length: 8192\n",
            "test-model",
            {"provider": "custom", "base_url": "http://localhost:11434/v1", "api_key": ""},
        )
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "◆ النموذج:" in info
        assert "◆ المزوّد:" in info
        assert "◆ السياق:" in info
        assert "◆ نقطة النهاية:" in info
        assert "◆ Model:" not in info


class TestResetNoticeSessionInfo:
    """#59003: the auto-reset banner must report the serving profile's config,
    not the multiplexer's base config."""

    _RUNTIME = {"provider": "", "base_url": "", "api_key": ""}

    def _source(self):
        from gateway.config import Platform
        from gateway.session import SessionSource
        return SessionSource(
            platform=Platform.TELEGRAM, chat_id="123", user_id="u1",
            profile="planner",
        )

    def _homes(self, tmp_path):
        base = tmp_path / "base"
        profile = tmp_path / "profiles" / "planner"
        profile.mkdir(parents=True)
        base.mkdir()
        base.joinpath("config.yaml").write_text(
            "display:\n  language: en\n"
            "model:\n  default: base-model\n  provider: custom\n  context_length: 1000\n")
        profile.joinpath("config.yaml").write_text(
            "display:\n  language: zh\n"
            "model:\n  default: profile-model\n  provider: anthropic\n  context_length: 2000\n")
        return base, profile

    @pytest.mark.parametrize(
        ("reason", "idle_minutes", "expected_reason"),
        [
            ("suspended", 60, "previous session was stopped or interrupted"),
            (
                "resume_pending_expired",
                60,
                "gateway restart recovery timed out",
            ),
            ("daily", 60, "daily schedule at 9:00"),
            ("idle", 120, "inactive for 2h"),
            ("idle", 150, "inactive for 2h 30m"),
            ("idle", 30, "inactive for 30m"),
        ],
    )
    def test_english_auto_reset_notice_preserves_legacy_output(
        self,
        runner,
        monkeypatch,
        reason,
        idle_minutes,
        expected_reason,
    ):
        from types import SimpleNamespace

        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        policy = SimpleNamespace(at_hour=9, idle_minutes=idle_minutes)
        with patch.object(GatewayRunner, "_format_session_info", return_value=""):
            notice = runner._format_auto_reset_notice_scoped(reason, policy)

        assert notice == (
            f"◐ Session automatically reset ({expected_reason}). "
            "Conversation history cleared.\n"
            "Use /resume to browse and restore a previous session.\n"
            "Adjust reset timing in config.yaml under session_reset."
        )

    def test_multiplex_uses_profile_config(self, runner, tmp_path, monkeypatch):
        from agent import i18n
        from types import SimpleNamespace

        monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
        i18n.reset_language_cache()
        base, profile = self._homes(tmp_path)
        runner.config = SimpleNamespace(multiplex_profiles=True)
        with patch("gateway.run._hermes_home", base), \
             patch.object(GatewayRunner, "_resolve_profile_home_for_source", return_value=profile), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value=self._RUNTIME):
            info = runner._reset_notice_session_info(self._source())
        assert "profile-model" in info
        assert "anthropic" in info
        assert "base-model" not in info
        assert "◆ 模型：" in info
        assert "◆ Model:" not in info

    def test_multiplex_auto_reset_notice_uses_profile_language(
        self, runner, tmp_path, monkeypatch
    ):
        from agent import i18n
        from types import SimpleNamespace

        monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
        i18n.reset_language_cache()
        base, profile = self._homes(tmp_path)
        runner.config = SimpleNamespace(multiplex_profiles=True)
        policy = SimpleNamespace(at_hour=9, idle_minutes=150)

        with patch("gateway.run._hermes_home", base), \
             patch.object(
                 GatewayRunner,
                 "_resolve_profile_home_for_source",
                 return_value=profile,
             ), \
             patch(
                 "gateway.run._resolve_runtime_agent_kwargs",
                 return_value=self._RUNTIME,
             ):
            notice = runner._format_auto_reset_notice(
                self._source(), "idle", policy
            )

        assert "会话已自动重置" in notice
        assert "闲置 2 小时 30 分钟" in notice
        assert "◆ 模型：" in notice
        assert "Session automatically reset" not in notice

    def test_arabic_auto_reset_notice(self, runner, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setenv("HERMES_LANGUAGE", "ar")
        policy = SimpleNamespace(at_hour=9, idle_minutes=150)
        with patch.object(GatewayRunner, "_format_session_info", return_value=""):
            notice = runner._format_auto_reset_notice_scoped("idle", policy)

        assert "تمت إعادة ضبط الجلسة تلقائيًا" in notice
        assert "عدم النشاط لمدة 2 ساعة و30 دقيقة" in notice
        assert "استخدم /resume" in notice
        assert "Session automatically reset" not in notice

    def test_resume_recovery_reason_is_localized(
        self, runner, tmp_path, monkeypatch
    ):
        from agent import i18n
        from types import SimpleNamespace

        monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
        i18n.reset_language_cache()
        _base, profile = self._homes(tmp_path)
        runner.config = SimpleNamespace(multiplex_profiles=True)
        policy = SimpleNamespace(at_hour=9, idle_minutes=60)

        with patch.object(
            GatewayRunner,
            "_resolve_profile_home_for_source",
            return_value=profile,
        ), patch.object(GatewayRunner, "_format_session_info", return_value=""):
            notice = runner._format_auto_reset_notice(
                self._source(), "resume_pending_expired", policy
            )

        assert "网关重启恢复超时" in notice
