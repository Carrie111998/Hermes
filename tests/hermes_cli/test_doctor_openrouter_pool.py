"""Regression tests for OpenRouter credential-pool detection in Doctor."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_cli.doctor import _resolve_openrouter_doctor_key


def test_doctor_resolves_manual_openrouter_pool_credential_without_rotation():
    pool = MagicMock()
    pool.has_credentials.return_value = True
    pool.peek.return_value = SimpleNamespace(access_token="manual-pool-key")

    with (
        patch("hermes_cli.config.get_env_value_prefer_dotenv", return_value=None),
        patch("agent.credential_pool.load_pool", return_value=pool) as load_pool,
    ):
        assert _resolve_openrouter_doctor_key() == "manual-pool-key"

    load_pool.assert_called_once_with("openrouter")
    pool.peek.assert_called_once_with()
    pool.select.assert_not_called()


def test_doctor_prefers_dotenv_openrouter_key_without_loading_pool():
    with (
        patch(
            "hermes_cli.config.get_env_value_prefer_dotenv",
            return_value="dotenv-key",
        ),
        patch("agent.credential_pool.load_pool") as load_pool,
    ):
        assert _resolve_openrouter_doctor_key() == "dotenv-key"

    load_pool.assert_not_called()


def test_doctor_accepts_runtime_api_key_from_pool_entry():
    pool = MagicMock()
    pool.has_credentials.return_value = True
    pool.peek.return_value = SimpleNamespace(
        runtime_api_key="runtime-pool-key",
        access_token="stored-token",
    )

    with (
        patch("hermes_cli.config.get_env_value_prefer_dotenv", return_value=None),
        patch("agent.credential_pool.load_pool", return_value=pool),
    ):
        assert _resolve_openrouter_doctor_key() == "runtime-pool-key"


def test_doctor_rejects_placeholder_pool_secret():
    pool = MagicMock()
    pool.has_credentials.return_value = True
    pool.peek.return_value = SimpleNamespace(access_token="placeholder")

    with (
        patch("hermes_cli.config.get_env_value_prefer_dotenv", return_value=None),
        patch("agent.credential_pool.load_pool", return_value=pool),
    ):
        assert _resolve_openrouter_doctor_key() == ""


def test_doctor_returns_empty_for_empty_pool_without_peeking():
    pool = MagicMock()
    pool.has_credentials.return_value = False

    with (
        patch("hermes_cli.config.get_env_value_prefer_dotenv", return_value=None),
        patch("agent.credential_pool.load_pool", return_value=pool),
    ):
        assert _resolve_openrouter_doctor_key() == ""

    pool.peek.assert_not_called()


def test_doctor_returns_empty_when_auth_store_is_unreadable():
    with (
        patch("hermes_cli.config.get_env_value_prefer_dotenv", return_value=None),
        patch("agent.credential_pool.load_pool", side_effect=OSError("locked")),
    ):
        assert _resolve_openrouter_doctor_key() == ""
