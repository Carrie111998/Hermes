"""Approval policy tests for ``hermes -z`` one-shot sessions."""

import os

from hermes_cli.oneshot import _configure_oneshot_yolo


def test_oneshot_yolo_defaults_fail_closed(monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"approvals": {}})

    assert _configure_oneshot_yolo() is False
    assert "HERMES_YOLO_MODE" not in os.environ


def test_oneshot_yolo_can_be_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"approvals": {"oneshot_yolo": True}},
    )

    assert _configure_oneshot_yolo() is True
    assert os.environ["HERMES_YOLO_MODE"] == "1"


def test_oneshot_yolo_fails_closed_when_config_load_errors(monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")

    def broken_config():
        raise RuntimeError("unreadable config")

    monkeypatch.setattr("hermes_cli.config.load_config", broken_config)

    assert _configure_oneshot_yolo() is False
    assert "HERMES_YOLO_MODE" not in os.environ