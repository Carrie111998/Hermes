"""API-server requests use one immutable listener-owned profile identity.

Every API request now executes under an immutable listener-owned profile
identity, including single-profile listeners. This keeps config/secrets and
durable stores on the same filesystem generation if a profile alias changes.

Adapted from PR #61283 by @giggling-ginger (originally targeting a
pre-``_profile_scope`` helper); no live gateway or network.
"""

from __future__ import annotations

import pytest

from agent import secret_scope as ss
from gateway.api_request_scope import capture_api_profile_identity
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


@pytest.fixture
def adapter():
    return APIServerAdapter(PlatformConfig(enabled=True))


class TestProfileScopeDefaultFallback:
    def test_single_profile_installs_frozen_scope(
        self,
        adapter,
        tmp_path,
        monkeypatch,
    ):
        (tmp_path / ".env").write_text(
            "OPENROUTER_BASE_URL=https://single.example/v1\n",
            encoding="utf-8",
        )
        identity = capture_api_profile_identity("default", tmp_path)
        adapter._api_profile_inventory = (identity,)
        monkeypatch.setenv(
            "OPENROUTER_BASE_URL",
            "https://from-environ.example/v1",
        )
        with adapter._profile_scope(None):
            assert ss.current_secret_scope() is not None
            assert (
                ss.get_secret("OPENROUTER_BASE_URL")
                == "https://single.example/v1"
            )
        assert ss.current_secret_scope() is None

    def test_single_profile_alias_retarget_fails_before_reading_new_home(
        self,
        adapter,
        tmp_path,
    ):
        old_home = tmp_path / "old"
        new_home = tmp_path / "new"
        old_home.mkdir()
        new_home.mkdir()
        (old_home / ".env").write_text(
            "OPENROUTER_BASE_URL=https://old.example/v1\n",
            encoding="utf-8",
        )
        (new_home / ".env").write_text(
            "OPENROUTER_BASE_URL=https://new.example/v1\n",
            encoding="utf-8",
        )
        alias = tmp_path / "active"
        alias.symlink_to(old_home, target_is_directory=True)
        identity = capture_api_profile_identity("default", alias)
        adapter._api_profile_inventory = (identity,)

        with adapter._profile_scope(None):
            assert (
                ss.get_secret("OPENROUTER_BASE_URL")
                == "https://old.example/v1"
            )

        alias.unlink()
        alias.symlink_to(new_home, target_is_directory=True)
        with pytest.raises(Exception, match="restart required"):
            with adapter._profile_scope(None):
                raise AssertionError("new profile home must never be entered")
