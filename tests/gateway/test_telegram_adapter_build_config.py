from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import (
    _build_adapter,
    _resolve_notifications_mode,
)


def test_adapter_build_uses_cached_gateway_config_not_full_loader(monkeypatch):
    import gateway.config as gateway_config
    import gateway.run as gateway_run

    config = PlatformConfig(enabled=True, token="test-token")
    monkeypatch.delenv("HERMES_TELEGRAM_NOTIFICATIONS", raising=False)
    monkeypatch.setattr(
        gateway_config,
        "load_gateway_config",
        lambda: (_ for _ in ()).throw(
            AssertionError("adapter construction must not use the full config loader")
        ),
    )
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"display": {"platforms": {"telegram": {"notifications": "all"}}}},
    )

    adapter = _build_adapter(config)

    assert adapter._notifications_mode == "all"


def test_notification_env_override_keeps_precedence(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_NOTIFICATIONS", "important")

    assert _resolve_notifications_mode() == "important"


def test_invalid_cached_mode_fails_closed(caplog, monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.delenv("HERMES_TELEGRAM_NOTIFICATIONS", raising=False)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "display": {"platforms": {"telegram": {"notifications": "unexpected"}}}
        },
    )

    assert _resolve_notifications_mode() == "important"
    assert "Unknown telegram notifications mode" in caplog.text


def test_missing_mode_keeps_existing_default(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.delenv("HERMES_TELEGRAM_NOTIFICATIONS", raising=False)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})

    assert _resolve_notifications_mode() == "important"
