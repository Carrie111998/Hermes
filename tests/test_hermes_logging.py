"""Tests for Hermes' Google Cloud Logging-compatible stream handler."""

import io
import json
import logging
import os
from pathlib import Path

import pytest

import hermes_logging


@pytest.fixture(autouse=True)
def _reset_logging_state():
    root = logging.getLogger()
    previous_level = root.level
    hermes_logging._reset_queued_handlers()
    hermes_logging._logging_initialized = False
    root.setLevel(logging.NOTSET)
    yield
    hermes_logging._reset_queued_handlers()
    root.setLevel(previous_level)
    hermes_logging._logging_initialized = False
    hermes_logging.clear_session_context()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = Path(os.environ["HERMES_HOME"])
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _structured_records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


class TestGCPStructuredLogFormatter:
    def test_formats_cloud_logging_fields(self):
        formatter = hermes_logging.GCPStructuredLogFormatter()
        record = logging.LogRecord(
            "gateway.run", logging.INFO, __file__, 1, "started %s", ("gateway",), None
        )

        payload = json.loads(formatter.format(record))

        assert payload["severity"] == "INFO"
        assert payload["message"] == "started gateway"
        assert payload["logger"] == "gateway.run"
        assert payload["time"].endswith("Z")
        assert isinstance(payload["pid"], int)
        assert isinstance(payload["thread"], int)

    @pytest.mark.parametrize(
        ("level", "severity"),
        [
            (logging.DEBUG, "DEBUG"),
            (logging.INFO, "INFO"),
            (logging.WARNING, "WARNING"),
            (logging.ERROR, "ERROR"),
            (logging.CRITICAL, "CRITICAL"),
        ],
    )
    def test_maps_python_levels(self, level, severity):
        formatter = hermes_logging.GCPStructuredLogFormatter()
        record = logging.LogRecord("test", level, __file__, 1, "message", (), None)

        assert json.loads(formatter.format(record))["severity"] == severity

    def test_includes_session_id(self):
        hermes_logging.set_session_context("session-123")
        formatter = hermes_logging.GCPStructuredLogFormatter()
        record = logging.getLogger("agent").makeRecord(
            "agent", logging.INFO, __file__, 1, "message", (), None
        )

        payload = json.loads(formatter.format(record))

        assert payload["session_id"] == "session-123"

    def test_includes_exception_in_message(self):
        formatter = hermes_logging.GCPStructuredLogFormatter()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            record = logging.LogRecord("agent", logging.ERROR, __file__, 1, "failed", (), None)
            record.exc_info = __import__("sys").exc_info()

        payload = json.loads(formatter.format(record))

        assert "failed" in payload["message"]
        assert "RuntimeError: boom" in payload["message"]


class TestSetupLogging:
    def test_installs_default_stderr_handler_without_creating_files(self, hermes_home, monkeypatch):
        stream = io.StringIO()
        monkeypatch.setattr(hermes_logging.sys, "stderr", stream)

        result = hermes_logging.setup_logging(hermes_home=hermes_home)
        logging.getLogger("test.setup").info("hello")
        hermes_logging.flush_log_queue()

        handlers = [
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_gcp_structured", False)
        ]
        assert result == hermes_home / "logs"
        assert len(handlers) == 1
        assert "INFO test.setup: hello" in stream.getvalue()
        assert not (hermes_home / "logs").exists()

    def test_uses_gcp_json_when_configured(self, hermes_home, monkeypatch):
        import yaml

        (hermes_home / "config.yaml").write_text(
            yaml.dump({"logging": {"format": "gcp_json"}})
        )
        stream = io.StringIO()
        monkeypatch.setattr(hermes_logging.sys, "stderr", stream)

        hermes_logging.setup_logging(hermes_home=hermes_home)
        logging.getLogger("test.setup").info("hello")
        hermes_logging.flush_log_queue()

        payload = _structured_records(stream)[-1]
        assert payload["severity"] == "INFO"
        assert payload["message"] == "hello"

    def test_is_idempotent(self, hermes_home, monkeypatch):
        monkeypatch.setattr(hermes_logging.sys, "stderr", io.StringIO())

        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_logging(hermes_home=hermes_home)

        handlers = [
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_gcp_structured", False)
        ]
        assert len(handlers) == 1

    def test_reads_log_level_from_config(self, hermes_home, monkeypatch):
        import yaml

        (hermes_home / "config.yaml").write_text(yaml.dump({"logging": {"level": "WARNING"}}))
        monkeypatch.setattr(hermes_logging.sys, "stderr", io.StringIO())

        hermes_logging.setup_logging(hermes_home=hermes_home)

        handler = next(
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_gcp_structured", False)
        )
        assert handler.level == logging.WARNING

    def test_verbose_reuses_shared_handler(self, hermes_home, monkeypatch):
        monkeypatch.setattr(hermes_logging.sys, "stderr", io.StringIO())
        hermes_logging.setup_logging(hermes_home=hermes_home)

        hermes_logging.setup_verbose_logging()

        handlers = [
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_gcp_structured", False)
        ]
        assert len(handlers) == 1
        assert handlers[0].level == logging.DEBUG


def test_filter_and_log_helpers_remain_compatible():
    assert hermes_logging._ComponentFilter(("gateway",)).filter(
        logging.LogRecord("gateway.run", logging.INFO, "", 0, "msg", (), None)
    )
    assert hermes_logging.rotating_file_handlers() == []
