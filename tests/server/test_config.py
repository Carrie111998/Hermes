"""Behavioral settings for the interfaze API come from config.yaml."""

import textwrap

import pytest

from server.config import Settings


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _write_config(home, body: str) -> None:
    (home / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


def test_document_settings_have_defaults(home):
    settings = Settings.load()
    assert settings.document_workers == 2
    assert settings.document_processing_timeout_seconds == 180
    assert settings.document_output_max_bytes == 50 * 1024 * 1024


def test_document_settings_read_from_config_yaml(home):
    _write_config(
        home,
        """
        interfaze_server:
          document_workers: 6
          document_processing_timeout_seconds: 45
          document_output_max_bytes: 1048576
        """,
    )
    settings = Settings.load()
    assert settings.document_workers == 6
    assert settings.document_processing_timeout_seconds == 45
    assert settings.document_output_max_bytes == 1048576


def test_document_settings_are_clamped_to_usable_values(home):
    _write_config(
        home,
        """
        interfaze_server:
          document_workers: 0
          document_processing_timeout_seconds: -5
          document_output_max_bytes: -1
        """,
    )
    settings = Settings.load()
    assert settings.document_workers == 1
    assert settings.document_processing_timeout_seconds >= 1
    assert settings.document_output_max_bytes >= 1


def test_document_settings_are_not_environment_variables(home, monkeypatch):
    """They are behavior, not deployment wiring — env must not override."""
    monkeypatch.setenv("INTERFAZE_DOCUMENT_WORKERS", "99")
    assert Settings.load().document_workers == 2


def test_malformed_config_falls_back_to_defaults(home):
    (home / "config.yaml").write_text("interfaze_server: [not, a, mapping]", encoding="utf-8")
    assert Settings.load().document_workers == 2


def test_bright_data_secret_comes_from_environment_and_behavior_from_config(home, monkeypatch):
    _write_config(
        home,
        """
        interfaze_server:
          brightdata_enabled: true
          brightdata_unlocker_zone: configured-zone
          brightdata_api_key: must-not-be-read-from-config
        """,
    )
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "environment-secret")

    settings = Settings.load()

    assert settings.brightdata_api_key == "environment-secret"
    assert settings.brightdata_enabled is True
    assert settings.brightdata_unlocker_zone == "configured-zone"


def test_bright_data_behavior_is_not_read_from_environment(home, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_ENABLED", "true")
    monkeypatch.setenv("BRIGHTDATA_UNLOCKER_ZONE", "environment-zone")

    settings = Settings.load()

    assert settings.brightdata_enabled is False
    assert settings.brightdata_unlocker_zone == "cli_unlocker"
