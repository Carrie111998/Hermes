"""Tests for RC1 (env file merge) and RC3 (spurious-change fix) in #70606."""

from pathlib import Path

import pytest

from plugins.memory.hindsight import (
    _embedded_config_changed,
    _embedded_profile_env_path,
    _load_simple_env,
    _materialize_embedded_profile_env,
)


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    """Redirect the embedded-profile env path into ``tmp_path``.

    ``_embedded_profile_env_path()`` resolves through ``Path.home()``. On
    Windows that reads USERPROFILE/HOMEDRIVE+HOMEPATH and ignores the POSIX
    ``HOME`` alias, so patching the env var alone would let these tests write
    into a real home directory. Patch the API itself — matching the fixture in
    ``test_hindsight_provider.py`` — and keep ``HOME`` set for any code that
    reads it directly.
    """
    h = tmp_path / "user-home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    return h


class TestEmbeddedProfileEnvMerge:
    """RC1: env file write must preserve user-added keys, not clobber them."""

    def test_fresh_install_writes_hermes_keys(self, user_home):
        config = {
            "profile": "hermes",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        }
        _materialize_embedded_profile_env(config)
        env_path = _embedded_profile_env_path(config)
        assert env_path.exists()
        saved = _load_simple_env(env_path)
        assert saved["HINDSIGHT_API_LLM_PROVIDER"] == "openai"
        assert saved["HINDSIGHT_API_LLM_MODEL"] == "gpt-4o-mini"
        assert saved["HINDSIGHT_API_LOG_LEVEL"] == "info"

    def test_user_keys_survive_rotation(self, user_home):
        config = {
            "profile": "hermes",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        }
        env_path = _embedded_profile_env_path(config)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "HINDSIGHT_API_LLM_PROVIDER=openai\n"
            "HINDSIGHT_API_LLM_MODEL=gpt-4o-mini\n"
            "HINDSIGHT_API_LLM_API_KEY=old-key\n"
            "HINDSIGHT_API_LOG_LEVEL=info\n"
            "# User keys below\n"
            "HINDSIGHT_EMBEDDING_PROVIDER=fastembed\n"
            "HINDSIGHT_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5\n"
            "HINDSIGHT_ONNX_MAX_BATCH=128\n"
            "HINDSIGHT_RERANKER_PROVIDER=jina\n",
            encoding="utf-8",
        )
        _materialize_embedded_profile_env(config, llm_api_key="new-key")
        saved = _load_simple_env(env_path)
        # Hermes-managed keys should rotate
        assert saved["HINDSIGHT_API_LLM_API_KEY"] == "new-key"
        assert saved["HINDSIGHT_API_LLM_PROVIDER"] == "openai"
        # User keys survive
        assert saved["HINDSIGHT_EMBEDDING_PROVIDER"] == "fastembed"
        assert saved["HINDSIGHT_EMBEDDING_MODEL"] == "BAAI/bge-small-en-v1.5"
        assert saved["HINDSIGHT_ONNX_MAX_BATCH"] == "128"
        assert saved["HINDSIGHT_RERANKER_PROVIDER"] == "jina"

    def test_hermes_key_value_wins_on_conflict(self, user_home):
        config = {
            "profile": "hermes",
            "llm_provider": "openai",
            "llm_model": "gpt-4o",
        }
        env_path = _embedded_profile_env_path(config)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        # User manually edited a Hermes key
        env_path.write_text(
            "HINDSIGHT_API_LLM_PROVIDER=openai\n"
            "HINDSIGHT_API_LLM_MODEL=gpt-4o-mini\n"
            "HINDSIGHT_API_LLM_API_KEY=key\n"
            "HINDSIGHT_API_LOG_LEVEL=debug\n"
            "HINDSIGHT_EMBEDDING_PROVIDER=fastembed\n",
            encoding="utf-8",
        )
        _materialize_embedded_profile_env(config)
        saved = _load_simple_env(env_path)
        # Config wins
        assert saved["HINDSIGHT_API_LLM_MODEL"] == "gpt-4o"
        # User value wins
        assert saved["HINDSIGHT_EMBEDDING_PROVIDER"] == "fastembed"


class TestEmbeddedConfigChanged:
    """RC3: user keys must not trigger spurious daemon restarts."""

    def test_no_change_when_user_keys_differ(self, user_home):
        config = {
            "profile": "hermes",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        }
        env_path = _embedded_profile_env_path(config)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "HINDSIGHT_API_LLM_PROVIDER=openai\n"
            "HINDSIGHT_API_LLM_MODEL=gpt-4o-mini\n"
            "HINDSIGHT_API_LLM_API_KEY=\n"
            "HINDSIGHT_API_LOG_LEVEL=info\n"
            "HINDSIGHT_EMBEDDING_PROVIDER=fastembed\n"
            "HINDSIGHT_ONNX_MAX_BATCH=128\n",
            encoding="utf-8",
        )
        assert not _embedded_config_changed(config)

    def test_change_when_hermes_key_differs(self, user_home):
        config = {
            "profile": "hermes",
            "llm_provider": "openai_compatible",
            "llm_model": "gpt-4o",
            "llm_base_url": "http://localhost:8000/v1",
        }
        env_path = _embedded_profile_env_path(config)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "HINDSIGHT_API_LLM_PROVIDER=openai\n"
            "HINDSIGHT_API_LLM_MODEL=gpt-4o-mini\n"
            "HINDSIGHT_API_LLM_API_KEY=\n"
            "HINDSIGHT_API_LOG_LEVEL=info\n"
            "HINDSIGHT_API_LLM_BASE_URL=http://localhost:8000/v1\n"
            "HINDSIGHT_EMBEDDING_PROVIDER=fastembed\n",
            encoding="utf-8",
        )
        # Model differs (gpt-4o-mini vs gpt-4o)
        assert _embedded_config_changed(config)

    def test_no_change_when_no_env_file_and_no_base_url(self, user_home):
        config = {
            "profile": "hermes",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        }
        # No env file yet, but that's expected on first run
        assert _embedded_config_changed(config)

    def test_change_when_base_url_added(self, user_home):
        config = {
            "profile": "hermes",
            "llm_provider": "openai_compatible",
            "llm_model": "gpt-4o-mini",
            "llm_base_url": "http://localhost:8000/v1",
        }
        env_path = _embedded_profile_env_path(config)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "HINDSIGHT_API_LLM_PROVIDER=openai\n"
            "HINDSIGHT_API_LLM_MODEL=gpt-4o-mini\n"
            "HINDSIGHT_API_LLM_API_KEY=\n"
            "HINDSIGHT_API_LOG_LEVEL=info\n",
            encoding="utf-8",
        )
        # base_url now in config but not in file
        assert _embedded_config_changed(config)


class TestEmbeddedConfigPresentToAbsent:
    """RC4: Hermes-owned keys removed from config must be deleted from env file."""

    def test_detects_removed_base_url(self, user_home):
        """When llm_base_url is removed from config, _embedded_config_changed should return True."""
        config = {
            "profile": "hermes",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        }
        env_path = _embedded_profile_env_path(config)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        # Old env file has base_url
        env_path.write_text(
            "HINDSIGHT_API_LLM_PROVIDER=openai\n"
            "HINDSIGHT_API_LLM_MODEL=gpt-4o-mini\n"
            "HINDSIGHT_API_LLM_API_KEY=\n"
            "HINDSIGHT_API_LOG_LEVEL=info\n"
            "HINDSIGHT_API_LLM_BASE_URL=http://old.api/v1\n"
            "HINDSIGHT_EMBEDDING_PROVIDER=fastembed\n",
            encoding="utf-8",
        )
        # Config no longer has llm_base_url → should detect change
        assert _embedded_config_changed(config)

    def test_removes_base_url_on_materialize(self, user_home):
        """When llm_base_url is removed, _materialize should delete HINDSIGHT_API_LLM_BASE_URL."""
        config = {
            "profile": "hermes",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        }
        env_path = _embedded_profile_env_path(config)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        # Old env file has base_url + user keys
        env_path.write_text(
            "HINDSIGHT_API_LLM_PROVIDER=openai\n"
            "HINDSIGHT_API_LLM_MODEL=gpt-4o-mini\n"
            "HINDSIGHT_API_LLM_API_KEY=\n"
            "HINDSIGHT_API_LOG_LEVEL=info\n"
            "HINDSIGHT_API_LLM_BASE_URL=http://old.api/v1\n"
            "HINDSIGHT_EMBEDDING_PROVIDER=fastembed\n"
            "HINDSIGHT_ONNX_MAX_BATCH=128\n",
            encoding="utf-8",
        )
        _materialize_embedded_profile_env(config)
        saved = _load_simple_env(env_path)
        # Hermes key removed
        assert "HINDSIGHT_API_LLM_BASE_URL" not in saved
        # User keys survive
        assert saved["HINDSIGHT_EMBEDDING_PROVIDER"] == "fastembed"
        assert saved["HINDSIGHT_ONNX_MAX_BATCH"] == "128"

    def test_removes_idle_timeout_on_materialize(self, user_home):
        """When idle_timeout is removed, _materialize should delete HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT."""
        config = {
            "profile": "hermes",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        }
        env_path = _embedded_profile_env_path(config)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        # Old env file has idle_timeout
        env_path.write_text(
            "HINDSIGHT_API_LLM_PROVIDER=openai\n"
            "HINDSIGHT_API_LLM_MODEL=gpt-4o-mini\n"
            "HINDSIGHT_API_LLM_API_KEY=\n"
            "HINDSIGHT_API_LOG_LEVEL=info\n"
            "HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT=600\n"
            "HINDSIGHT_RERANKER_PROVIDER=jina\n",
            encoding="utf-8",
        )
        _materialize_embedded_profile_env(config)
        saved = _load_simple_env(env_path)
        # Hermes key removed
        assert "HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT" not in saved
        # User key survives
        assert saved["HINDSIGHT_RERANKER_PROVIDER"] == "jina"


