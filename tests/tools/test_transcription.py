"""Tests for transcription_tools.py — local (faster-whisper) and OpenAI providers.

Tests cover provider selection, config loading, validation, and transcription
dispatch.  All external dependencies (faster_whisper, openai) are mocked.
"""

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _fake_faster_whisper_module(mock_model):
    return SimpleNamespace(WhisperModel=MagicMock(return_value=mock_model))


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.usefixtures("disable_lazy_stt_install")


@pytest.fixture(autouse=True)
def _clear_openai_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


class TestGetProvider:
    """_get_provider() picks the right backend based on config + availability."""

    def test_local_when_available(self):
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True):
            from tools.transcription_tools import _get_provider
            assert _get_provider({"provider": "local"}) == "local"

    def test_explicit_local_no_cloud_fallback(self, monkeypatch):
        """Explicit local provider must not silently fall back to cloud."""
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-test")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False), \
             patch("tools.transcription_tools._HAS_OPENAI", True), \
             patch("tools.transcription_tools._has_local_command", return_value=False), \
             patch("tools.tool_backend_helpers.read_selection", return_value="local"):
            from tools.transcription_tools import _get_provider
            assert _get_provider({"provider": "local"}) == "none"


    def test_disabled_config_returns_none(self):
        from tools.transcription_tools import _get_provider
        assert _get_provider({"enabled": False, "provider": "openai"}) == "none"


class TestAutodetectPrefersManagedGatewayOverInstallingLocal:
    """Autodetect must never PROVISION on-host STT when the managed Nous Tool
    Gateway is available.

    Hermes Cloud instances ship no faster-whisper and set no stt.provider, so
    autodetect used to hit ``_try_lazy_install_stt()`` and download a model
    onto a small instance volume. Live failure (staging 2026-08-26,
    hermes-agent-stg-test-6698): faster-whisper's 145 MB Systran model could
    not be fetched onto a 98%-full disk, so every voice note fell back to a
    "transcribe it yourself" note and the agent hand-rolled a pipeline.

    The narrow contract deliberately does NOT take local away from anyone who
    already has it: an entitled self-hosted user with faster-whisper installed
    keeps their free, private, local model. Only the INSTALL is suppressed,
    and only when a managed backend is actually ready.
    """

    def _autodetect(self, monkeypatch):
        """Autodetect config: no explicit provider anywhere."""
        monkeypatch.setattr(
            "tools.tool_backend_helpers.read_selection", lambda _k: None
        )
        from tools.transcription_tools import _get_provider

        return lambda: _get_provider({"enabled": True})

    def test_no_local_installed_and_gateway_ready_uses_managed_not_install(
        self, monkeypatch
    ):
        """The Hermes Cloud shape: nothing local, gateway entitled."""
        installs: list[bool] = []

        def _fake_install():
            installs.append(True)
            return True

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False), patch(
            "tools.transcription_tools._has_local_command", return_value=False
        ), patch(
            "tools.transcription_tools._try_lazy_install_stt", _fake_install
        ), patch(
            "tools.transcription_tools._HAS_OPENAI", True
        ), patch(
            "tools.transcription_tools._has_openai_audio_backend", return_value=True
        ), patch(
            "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
            return_value=True,
        ):
            resolved = self._autodetect(monkeypatch)()

        assert resolved == "openai", (
            "an entitled instance with no local STT must use the managed gateway"
        )
        assert installs == [], (
            "must NOT lazy-install faster-whisper when the managed gateway is ready"
        )

    def test_managed_gateway_beats_an_installed_local_model(self, monkeypatch):
        """When the gateway is entitled it is the DEFAULT — even if
        faster-whisper happens to be importable.

        Package presence is not a user preference. On a hosted instance a
        local model is usually RESIDUE from the old lazy-install path, not a
        deliberate choice, and preferring it is what kept staging broken:
        faster_whisper sat in /opt/data/lazy-packages (on the data volume, not
        the venv), so the gateway resolved "local" at rung 1 and never reached
        the managed branch — every voice note died on a full disk.

        Opting out is explicit: set stt.provider (see the tests below).
        """
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), patch(
            "tools.transcription_tools._HAS_OPENAI", True
        ), patch(
            "tools.transcription_tools._has_openai_audio_backend", return_value=True
        ), patch(
            "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
            return_value=True,
        ):
            assert self._autodetect(monkeypatch)() == "openai"

    def test_managed_gateway_beats_a_local_command(self, monkeypatch):
        """Same precedence for HERMES_LOCAL_STT_COMMAND: the gateway is the
        default, and the command is honoured via explicit config."""
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False), patch(
            "tools.transcription_tools._has_local_command", return_value=True
        ), patch(
            "tools.transcription_tools._HAS_OPENAI", True
        ), patch(
            "tools.transcription_tools._has_openai_audio_backend", return_value=True
        ), patch(
            "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
            return_value=True,
        ):
            assert self._autodetect(monkeypatch)() == "openai"

    def test_explicit_local_provider_opts_out_of_the_gateway(self, monkeypatch):
        """The documented opt-out: stt.provider: local is honoured verbatim,
        gateway entitlement notwithstanding.

        Asserted via the EXPLICIT branch specifically: _has_local_command is
        False and the lazy-install is stubbed to fail, so "local" can only be
        returned by the explicit `provider == "local"` handler. Without that,
        this test would pass off the unentitled fallthrough ladder and would
        not actually pin the opt-out (caught by mutation testing).

        read_selection returns "local" because that is what a REAL opt-out
        looks like: it reads the raw config.yaml, so a hand-written or
        picker-written stt.provider surfaces here. (None means "never
        configured" — the legacy DEFAULT_CONFIG seed — which correctly falls
        through to autodetect and therefore to the gateway.)
        """
        monkeypatch.setattr(
            "tools.tool_backend_helpers.read_selection", lambda _k: "local"
        )
        from tools.transcription_tools import _get_provider

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), patch(
            "tools.transcription_tools._has_local_command", return_value=False
        ), patch(
            "tools.transcription_tools._try_lazy_install_stt", return_value=False
        ), patch(
            "tools.transcription_tools._HAS_OPENAI", True
        ), patch(
            "tools.transcription_tools._has_openai_audio_backend", return_value=True
        ), patch(
            "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
            return_value=True,
        ):
            assert _get_provider({"enabled": True, "provider": "local"}) == "local"

    def test_explicit_local_command_provider_opts_out_of_the_gateway(
        self, monkeypatch
    ):
        """Opt-out via an explicit local_command provider."""
        monkeypatch.setattr(
            "tools.tool_backend_helpers.read_selection", lambda _k: "local_command"
        )
        from tools.transcription_tools import _get_provider

        with patch("tools.transcription_tools._has_local_command", return_value=True), \
             patch(
                 "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
                 return_value=True,
             ):
            resolved = _get_provider({"enabled": True, "provider": "local_command"})
        assert resolved == "local_command"

    def test_no_gateway_still_lazy_installs_local(self, monkeypatch):
        """Unentitled/self-hosted with no local backend keeps today's
        behaviour exactly — the install is the only way they get STT."""
        installs: list[bool] = []

        def _fake_install():
            installs.append(True)
            return True

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False), patch(
            "tools.transcription_tools._has_local_command", return_value=False
        ), patch(
            "tools.transcription_tools._try_lazy_install_stt", _fake_install
        ), patch(
            "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
            return_value=False,
        ):
            resolved = self._autodetect(monkeypatch)()

        assert installs == [True], "unentitled boxes must still be able to install"
        assert resolved == "local"

    def test_managed_branch_does_not_claim_the_gateway_when_a_direct_key_wins(
        self, monkeypatch
    ):
        """Don't log "using the managed gateway" when the request won't go there.

        _resolve_openai_audio_client_config's legacy ladder (no stored stt
        selection) prefers a DIRECT OPENAI_API_KEY over the managed gateway.
        So on an entitled box that also has a direct key, resolving to
        "openai" is still correct — but it is NOT a managed-gateway decision,
        and the branch must not announce one. Suppressing the install is the
        part that matters, and it must still hold.
        """
        import logging

        installs: list[bool] = []

        def _fake_install():
            installs.append(True)
            return True

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False), patch(
            "tools.transcription_tools._has_local_command", return_value=False
        ), patch(
            "tools.transcription_tools._try_lazy_install_stt", _fake_install
        ), patch(
            "tools.transcription_tools._HAS_OPENAI", True
        ), patch(
            "tools.transcription_tools._has_openai_audio_backend", return_value=True
        ), patch(
            "tools.transcription_tools.resolve_openai_audio_api_key",
            return_value="sk-user-direct",
        ), patch(
            "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
            return_value=True,
        ), patch(
            "tools.tool_backend_helpers.read_selection", return_value=None
        ):
            from tools.transcription_tools import _get_provider

            with self._capture_logs() as records:
                resolved = _get_provider({"enabled": True})

        assert resolved == "openai"
        assert installs == [], "the install must still be suppressed"
        managed_claims = [
            r for r in records if "managed Nous Tool Gateway" in r.getMessage()
        ]
        assert not managed_claims, (
            "logged a managed-gateway claim while a direct key will actually "
            f"serve the request: {[r.getMessage() for r in managed_claims]}"
        )

    def _capture_logs(self):
        import contextlib
        import logging

        @contextlib.contextmanager
        def _cap():
            records: list[logging.LogRecord] = []

            class _H(logging.Handler):
                def emit(self, record):
                    records.append(record)

            logger = logging.getLogger("tools.transcription_tools")
            h = _H()
            logger.addHandler(h)
            prev = logger.level
            logger.setLevel(logging.INFO)
            try:
                yield records
            finally:
                logger.removeHandler(h)
                logger.setLevel(prev)

        return _cap()

    def test_unentitled_box_with_a_direct_key_still_prefers_local(
        self, monkeypatch
    ):
        """Entitlement is what unlocks the gateway-first precedence.

        A self-hosted user with OPENAI_API_KEY set but NO Nous entitlement has
        an openai audio backend available — but no managed gateway. They must
        keep the original local-first ladder, not be pushed onto their own
        metered OpenAI key. (Mutation testing caught that removing the
        _managed_stt_ready condition changed nothing without this test.)
        """
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), patch(
            "tools.transcription_tools._HAS_OPENAI", True
        ), patch(
            "tools.transcription_tools._has_openai_audio_backend", return_value=True
        ), patch(
            "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
            return_value=False,
        ):
            assert self._autodetect(monkeypatch)() == "local"

    def test_regression_staging_lazy_installed_whisper_on_full_disk(
        self, monkeypatch
    ):
        """Regression for the exact staging failure (2026-08-27).

        hermes-agent-stg-test-6698: faster_whisper had been lazy-installed into
        /opt/data/lazy-packages (the data volume — absent from the venv, so
        `pip show` reported nothing). The gateway process put that directory on
        sys.path, imported it, and resolved "local" at rung 1. Every voice note
        then failed inside the model download:

            Local transcription failed: Task error: File reconstruction error:
            IO Error: No space left on device (os error 28)

        ...while an entitled managed gateway sat idle, and the agent fell back
        to shelling out to ffmpeg. Package presence must NOT outrank an
        entitled gateway.
        """
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), patch(
            "tools.transcription_tools._has_local_command", return_value=False
        ), patch(
            "tools.transcription_tools._HAS_OPENAI", True
        ), patch(
            "tools.transcription_tools._has_openai_audio_backend", return_value=True
        ), patch(
            "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
            return_value=True,
        ):
            resolved = self._autodetect(monkeypatch)()

        assert resolved == "openai", (
            "lazy-installed faster-whisper must not outrank an entitled "
            "managed gateway — this is what kept staging broken"
        )

    def test_local_command_wins_when_no_gateway_is_available(self, monkeypatch):
        """Without entitlement nothing changes: local_command still wins."""
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False), patch(
            "tools.transcription_tools._has_local_command", return_value=True
        ), patch(
            "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
            return_value=False,
        ):
            assert self._autodetect(monkeypatch)() == "local_command"

    def test_installed_local_wins_when_no_gateway_is_available(self, monkeypatch):
        """Self-hosted, unentitled, faster-whisper present -> local, as today."""
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), patch(
            "tools.managed_tool_gateway.is_managed_tool_gateway_ready",
            return_value=False,
        ):
            assert self._autodetect(monkeypatch)() == "local"


# ---------------------------------------------------------------------------
# File validation
# ---------------------------------------------------------------------------


class TestValidateAudioFile:

    def test_missing_file(self, tmp_path):
        from tools.transcription_tools import _validate_audio_file
        result = _validate_audio_file(str(tmp_path / "nope.ogg"))
        assert result is not None
        assert "not found" in result["error"]


    def test_too_large(self, tmp_path):
        f = tmp_path / "big.ogg"
        f.write_bytes(b"x")
        from tools.transcription_tools import _validate_audio_file, MAX_FILE_SIZE
        real_stat = f.stat()
        with patch.object(type(f), "stat", return_value=os.stat_result((
            real_stat.st_mode, real_stat.st_ino, real_stat.st_dev,
            real_stat.st_nlink, real_stat.st_uid, real_stat.st_gid,
            MAX_FILE_SIZE + 1,  # st_size
            real_stat.st_atime, real_stat.st_mtime, real_stat.st_ctime,
        ))):
            result = _validate_audio_file(str(f))
        assert result is not None
        assert "too large" in result["error"]


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestLoadSttConfig:

    def test_merges_default_local_initial_prompt(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            "stt:\n  local:\n    model: small\n",
            encoding="utf-8",
        )

        from tools.transcription_tools import _load_stt_config
        local_config = _load_stt_config()["local"]

        assert local_config["model"] == "small"
        assert local_config["initial_prompt"] == ""


# ---------------------------------------------------------------------------
# Local transcription
# ---------------------------------------------------------------------------


class TestTranscribeLocal:

    def test_successful_transcription(self, tmp_path):
        audio_file = tmp_path / "test.ogg"
        audio_file.write_bytes(b"fake audio")

        mock_segment = MagicMock()
        mock_segment.text = "Hello world"
        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.duration = 2.5

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([mock_segment], mock_info)

        fake_fw = _fake_faster_whisper_module(mock_model)
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), \
             patch.dict("sys.modules", {"faster_whisper": fake_fw}), \
             patch("tools.transcription_tools._local_model", None):
            from tools.transcription_tools import _transcribe_local
            result = _transcribe_local(str(audio_file), "base")

        assert result["success"] is True
        assert result["transcript"] == "Hello world"


    def test_not_installed(self):
        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False):
            from tools.transcription_tools import _transcribe_local
            result = _transcribe_local("/tmp/test.ogg", "base")
        assert result["success"] is False
        assert "not installed" in result["error"]


# ---------------------------------------------------------------------------
# OpenAI transcription
# ---------------------------------------------------------------------------


class TestTranscribeOpenAI:

    def test_no_key(self, monkeypatch):
        monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
        from tools.transcription_tools import _transcribe_openai
        result = _transcribe_openai("/tmp/test.ogg", "whisper-1")
        assert result["success"] is False
        assert "VOICE_TOOLS_OPENAI_KEY" in result["error"]


    def test_unset_language_omits_argument(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-test")
        audio_file = tmp_path / "test.ogg"
        audio_file.write_bytes(b"fake audio")

        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.return_value = "Hello"

        with patch("tools.transcription_tools._HAS_OPENAI", True), \
             patch("tools.transcription_tools._load_stt_config", return_value={
                 "openai": {"language": ""},
             }), \
             patch("openai.OpenAI", return_value=mock_client):
            from tools.transcription_tools import _transcribe_openai
            result = _transcribe_openai(str(audio_file), "whisper-1")

        assert result["success"] is True
        assert "language" not in mock_client.audio.transcriptions.create.call_args.kwargs


# ---------------------------------------------------------------------------
# Main transcribe_audio() dispatch
# ---------------------------------------------------------------------------


class TestTranscribeAudio:

    def test_dispatches_to_local(self, tmp_path):
        audio_file = tmp_path / "test.ogg"
        audio_file.write_bytes(b"fake audio")

        with patch("tools.transcription_tools._load_stt_config", return_value={"provider": "local"}), \
             patch("tools.transcription_tools._get_provider", return_value="local"), \
             patch("tools.transcription_tools._transcribe_local", return_value={"success": True, "transcript": "hi"}) as mock_local:
            from tools.transcription_tools import transcribe_audio
            result = transcribe_audio(str(audio_file))

        assert result["success"] is True
        mock_local.assert_called_once()


    def test_invalid_file_returns_error(self):
        from tools.transcription_tools import transcribe_audio
        result = transcribe_audio("/nonexistent/file.ogg")
        assert result["success"] is False
        assert "not found" in result["error"]


class TestLocalFallback:

    def test_uses_installed_faster_whisper_without_changing_provider(self, tmp_path):
        audio_file = tmp_path / "test.ogg"
        audio_file.write_bytes(b"fake audio")

        with patch(
            "tools.transcription_tools._load_stt_config",
            return_value={"provider": "openai", "local": {"model": "small"}},
        ), patch(
            "tools.transcription_tools._HAS_FASTER_WHISPER",
            True,
        ), patch(
            "tools.transcription_tools._transcribe_local",
            return_value={"success": True, "transcript": "local result"},
        ) as mock_local:
            from tools.transcription_tools import transcribe_audio_local_fallback

            result = transcribe_audio_local_fallback(str(audio_file))

        assert result["transcript"] == "local result"
        mock_local.assert_called_once_with(str(audio_file), "small")

    def test_does_not_install_when_no_local_backend_exists(self, tmp_path):
        audio_file = tmp_path / "test.ogg"
        audio_file.write_bytes(b"fake audio")

        with patch("tools.transcription_tools._HAS_FASTER_WHISPER", False), patch(
            "tools.transcription_tools._has_local_command", return_value=False
        ):
            from tools.transcription_tools import transcribe_audio_local_fallback

            result = transcribe_audio_local_fallback(str(audio_file))

        assert result["success"] is False
        assert "installed local STT" in result["error"]


# ---------------------------------------------------------------------------
# Model name normalisation for local providers
# ---------------------------------------------------------------------------


class TestNormalizeLocalModel:
    """_normalize_local_model() maps cloud-only names to the local default."""

    def test_openai_model_name_maps_to_default(self):
        from tools.transcription_tools import _normalize_local_model, DEFAULT_LOCAL_MODEL
        assert _normalize_local_model("whisper-1") == DEFAULT_LOCAL_MODEL


    def test_local_transcribe_normalises_model(self):
        """transcribe_audio with local provider must not pass 'whisper-1' to WhisperModel."""
        import os
        from unittest.mock import MagicMock, patch

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"x")
            audio_file = f.name
        try:
            mock_model = MagicMock()
            mock_model.transcribe.return_value = (iter([]), MagicMock(language="en", duration=1.0))
            with patch("tools.transcription_tools._HAS_FASTER_WHISPER", True), \
                 patch("tools.transcription_tools._load_stt_config", return_value={
                     "enabled": True,
                     "provider": "local",
                     "local": {"model": "whisper-1"},
                 }), \
                 patch("tools.transcription_tools._local_model", None), \
                 patch("tools.transcription_tools._local_model_name", None), \
                 patch.dict("sys.modules", {"faster_whisper": _fake_faster_whisper_module(mock_model)}):
                mock_cls = __import__("faster_whisper").WhisperModel
                from tools.transcription_tools import transcribe_audio
                transcribe_audio(audio_file)
                # WhisperModel must NOT have been called with "whisper-1"
                call_args = mock_cls.call_args
                assert call_args is not None
                assert call_args[0][0] != "whisper-1", (
                    "WhisperModel was called with the cloud-only name 'whisper-1'"
                )
        finally:
            os.unlink(audio_file)
