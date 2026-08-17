"""Tests for hermes_logging — centralized logging setup."""
import io
import logging
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import hermes_logging
# Use whatever RotatingFileHandler class hermes_logging actually resolved so
# the autouse fixture's isinstance checks (which strip rotating handlers
# between tests) match the real handlers on every platform. hermes_logging
# aliases concurrent-log-handler's ConcurrentRotatingFileHandler on Windows
# (the #44873 fix) but keeps stdlib RotatingFileHandler on POSIX, so importing
# the name from the module under test keeps the two in lockstep.
from hermes_logging import RotatingFileHandler


def _assert_still_logging(handler, base: Path, marker: str) -> None:
    """Assert *handler* can still write to *base* after a rollover.

    This is the invariant the rollover tests actually care about: whether a
    rollover succeeded or was deferred by a lock, the handler must keep
    logging to the base file. Asserting ``handler.stream is not None`` or an
    eager ``base.exists()`` right after ``doRollover()`` instead tests stdlib
    *mechanics*, which concurrent-log-handler (aliased as
    ``RotatingFileHandler`` on Windows for #44873) deliberately inverts:

      * ``_actual_keep_log_stream_open`` is forced ``False`` on Windows, so
        ``handler.stream`` is ``None`` between writes — an open handle on the
        base file is precisely what makes ``os.rename`` fail with WinError 32,
        i.e. the bug CLH exists to fix.
      * ``_open()`` returns ``None`` and CLH constructs itself with
        ``delay=True``; the base file is (re)created lazily in ``do_write()``.

    Emitting and reading the file back proves both properties at once — the
    stream is usable AND the base file is there — on either implementation.
    """
    handler.emit(logging.makeLogRecord({"msg": marker}))
    handler.flush()
    assert base.exists(), f"{base.name} was not recreated after rollover"
    assert marker in base.read_text(encoding="utf-8"), (
        f"handler stopped logging to {base.name} after rollover"
    )


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """Reset the module-level sentinel and clean up root logger handlers
    added by setup_logging() so tests don't leak state.

    Under xdist (-n auto) other test modules may have called setup_logging()
    in the same worker process, leaving RotatingFileHandlers on the root
    logger.  We strip ALL RotatingFileHandlers before each test so the count
    assertions are stable regardless of test ordering.
    """
    hermes_logging._logging_initialized = False
    # File handlers now live behind the async QueueListener, not on the root
    # logger; tear down any leaked from other xdist tests in this worker.
    hermes_logging._reset_queued_handlers()
    root = logging.getLogger()
    prev_root_level = root.level
    root.setLevel(logging.NOTSET)
    # Snapshot the remaining (non-file) handlers so we can strip whatever the
    # test adds.
    pre_existing = list(root.handlers)
    # Ensure the record factory is installed (it's idempotent).
    hermes_logging._install_session_record_factory()
    yield
    # Restore — tear down async file logging + remove handlers added by the test.
    hermes_logging._reset_queued_handlers()
    for h in list(root.handlers):
        if h not in pre_existing:
            root.removeHandler(h)
            h.close()
    root.setLevel(prev_root_level)
    hermes_logging._logging_initialized = False
    hermes_logging.clear_session_context()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Provide an isolated HERMES_HOME for logging tests.

    Uses the same tmp_path as the autouse _isolate_hermes_home from conftest,
    reading it back from the env var to avoid double-mkdir conflicts.
    """
    home = Path(os.environ["HERMES_HOME"])
    return home


class TestSetupLogging:
    """setup_logging() creates agent.log + errors.log with RotatingFileHandler."""

    def test_creates_log_directory(self, hermes_home):
        log_dir = hermes_logging.setup_logging(hermes_home=hermes_home)
        assert log_dir == hermes_home / "logs"
        assert log_dir.is_dir()

    def test_creates_agent_log_handler(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        root = logging.getLogger()

        agent_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1
        assert agent_handlers[0].level == logging.INFO

    def test_creates_errors_log_handler(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        root = logging.getLogger()

        error_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and "errors.log" in getattr(h, "baseFilename", "")
        ]
        assert len(error_handlers) == 1
        assert error_handlers[0].level == logging.WARNING

    def test_idempotent_no_duplicate_handlers(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_logging(hermes_home=hermes_home)  # second call — should be no-op

        root = logging.getLogger()
        agent_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1

    def test_force_reinitializes(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        # Force still won't add duplicate handlers because _add_rotating_handler
        # checks by resolved path.
        hermes_logging.setup_logging(hermes_home=hermes_home, force=True)

        root = logging.getLogger()
        agent_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1

    def test_custom_log_level(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, log_level="DEBUG")

        root = logging.getLogger()
        agent_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].level == logging.DEBUG

    def test_custom_max_size_and_backup(self, hermes_home):
        hermes_logging.setup_logging(
            hermes_home=hermes_home, max_size_mb=10, backup_count=5
        )

        root = logging.getLogger()
        agent_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].maxBytes == 10 * 1024 * 1024
        assert agent_handlers[0].backupCount == 5

    def test_suppresses_noisy_loggers(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)

        assert logging.getLogger("openai").level >= logging.WARNING
        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING

    def test_writes_to_agent_log(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)

        test_logger = logging.getLogger("test_hermes_logging.write_test")
        test_logger.info("test message for agent.log")

        # Flush handlers
        hermes_logging.flush_log_queue()

        agent_log = hermes_home / "logs" / "agent.log"
        assert agent_log.exists()
        content = agent_log.read_text()
        assert "test message for agent.log" in content

    def test_warnings_appear_in_both_logs(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)

        test_logger = logging.getLogger("test_hermes_logging.warning_test")
        test_logger.warning("this is a warning")

        hermes_logging.flush_log_queue()

        agent_log = hermes_home / "logs" / "agent.log"
        errors_log = hermes_home / "logs" / "errors.log"
        assert "this is a warning" in agent_log.read_text()
        assert "this is a warning" in errors_log.read_text()

    def test_info_not_in_errors_log(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)

        test_logger = logging.getLogger("test_hermes_logging.info_test")
        test_logger.info("info only message")

        hermes_logging.flush_log_queue()

        errors_log = hermes_home / "logs" / "errors.log"
        if errors_log.exists():
            assert "info only message" not in errors_log.read_text()

    def test_reads_config_yaml(self, hermes_home):
        """setup_logging reads logging.level from config.yaml."""
        import yaml
        config = {"logging": {"level": "DEBUG", "max_size_mb": 2, "backup_count": 1}}
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        hermes_logging.setup_logging(hermes_home=hermes_home)

        root = logging.getLogger()
        agent_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].level == logging.DEBUG
        assert agent_handlers[0].maxBytes == 2 * 1024 * 1024
        assert agent_handlers[0].backupCount == 1

    def test_explicit_params_override_config(self, hermes_home):
        """Explicit function params take precedence over config.yaml."""
        import yaml
        config = {"logging": {"level": "DEBUG"}}
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        hermes_logging.setup_logging(hermes_home=hermes_home, log_level="WARNING")

        root = logging.getLogger()
        agent_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].level == logging.WARNING

    def test_record_factory_installed(self, hermes_home):
        """The custom record factory injects session_tag on all records."""
        hermes_logging.setup_logging(hermes_home=hermes_home)
        factory = logging.getLogRecordFactory()
        assert getattr(factory, "_hermes_session_injector", False), (
            "Record factory should have _hermes_session_injector marker"
        )
        # Verify session_tag exists on a fresh record
        record = factory("test", logging.INFO, "", 0, "msg", (), None)
        assert hasattr(record, "session_tag")


class TestGatewayMode:
    """setup_logging(mode='gateway') creates a filtered gateway.log."""

    def test_gateway_log_created(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        root = logging.getLogger()

        gw_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "gateway.log"
        ]
        assert len(gw_handlers) == 1

    def test_gateway_log_not_created_in_cli_mode(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="cli")
        root = logging.getLogger()

        gw_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "gateway.log"
        ]
        assert len(gw_handlers) == 0

    def test_gateway_log_created_after_cli_init(self, hermes_home):
        """Gateway mode attaches gateway.log even after earlier CLI init."""
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="cli")
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        root = logging.getLogger()
        gw_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "gateway.log"
        ]
        assert len(gw_handlers) == 1

        logging.getLogger("gateway.run").info("gateway connected after cli init")

        hermes_logging.flush_log_queue()

        gw_log = hermes_home / "logs" / "gateway.log"
        assert gw_log.exists()
        assert "gateway connected after cli init" in gw_log.read_text()

    def test_gateway_log_created_after_cli_init_without_duplicate_handlers(self, hermes_home):
        """Repeated gateway setup calls do not attach duplicate gateway handlers."""
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="cli")
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        root = logging.getLogger()
        gw_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "gateway.log"
        ]
        assert len(gw_handlers) == 1

    def test_gateway_log_receives_gateway_records(self, hermes_home):
        """gateway.log captures records from gateway.* loggers."""
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        gw_logger = logging.getLogger("plugins.platforms.telegram.adapter")
        gw_logger.info("telegram connected")

        hermes_logging.flush_log_queue()

        gw_log = hermes_home / "logs" / "gateway.log"
        assert gw_log.exists()
        assert "telegram connected" in gw_log.read_text()

    def test_gateway_log_rejects_non_gateway_records(self, hermes_home):
        """gateway.log does NOT capture records from tools.*, agent.*, etc."""
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        tool_logger = logging.getLogger("tools.terminal_tool")
        tool_logger.info("running command")

        agent_logger = logging.getLogger("agent.context_compressor")
        agent_logger.info("compressing context")

        hermes_logging.flush_log_queue()

        gw_log = hermes_home / "logs" / "gateway.log"
        if gw_log.exists():
            content = gw_log.read_text()
            assert "running command" not in content
            assert "compressing context" not in content

    def test_agent_log_still_receives_all(self, hermes_home):
        """The catch-all log still receives gateway AND tool records.

        With mode="gateway" the catch-all is agent-gateway.log (per-role
        routing so the gateway process is the sole holder of that file).
        """
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        gw_logger = logging.getLogger("gateway.run")
        file_logger = logging.getLogger("tools.file_tools")
        # Ensure propagation and levels are clean (cross-test pollution defense)
        gw_logger.propagate = True
        file_logger.propagate = True
        logging.getLogger("tools").propagate = True
        file_logger.setLevel(logging.NOTSET)
        logging.getLogger("tools").setLevel(logging.NOTSET)

        gw_logger.info("gateway msg")
        file_logger.info("file msg")

        hermes_logging.flush_log_queue()

        # mode="gateway" routes the catch-all to agent-gateway.log.
        agent_log = hermes_home / "logs" / "agent-gateway.log"
        content = agent_log.read_text()
        assert "gateway msg" in content
        assert "file msg" in content

    def test_gateway_handlers_added_after_cli_init(self, hermes_home, monkeypatch):
        """REGRESSION: production path calls setup_logging(mode="cli") at
        hermes_cli/main.py module import, then setup_logging(mode="gateway")
        from gateway/run.py. Pre-fix the second call was a silent no-op due
        to the global ``_logging_initialized`` guard, so gateway.log stopped
        being written (last record 2026-04-10).
        """
        monkeypatch.delenv("HERMES_GATEWAY_LOG_FILE", raising=False)
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="cli")
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        root = logging.getLogger()
        gw_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "gateway.log"
        ]
        assert len(gw_handlers) == 1, (
            "gateway.log handler MUST be attached even after a prior cli-mode "
            "init (regression: silently skipped pre-fix)"
        )


class TestGatewayForensicsLog:
    """When mode='gateway', an unfiltered forensics log captures every
    record at INFO+ from any logger, independent of stdout/stderr capture
    by the wrapper that spawned us. Path overridable via
    ``HERMES_GATEWAY_LOG_FILE`` env var.
    """

    def test_forensics_log_created_when_mode_gateway(self, hermes_home, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_LOG_FILE", raising=False)
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        root = logging.getLogger()

        forensics_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "gateway-forensics.log"
        ]
        assert len(forensics_handlers) == 1
        assert forensics_handlers[0].level == logging.INFO

    def test_forensics_log_not_created_in_cli_mode(self, hermes_home, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_LOG_FILE", raising=False)
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="cli")
        root = logging.getLogger()

        forensics_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "gateway-forensics.log"
        ]
        assert len(forensics_handlers) == 0

    def test_forensics_log_captures_non_gateway_loggers(self, hermes_home, monkeypatch):
        """Forensics is unfiltered — captures gateway.*, events.*, tools.*,
        and anything else that emits at INFO+. This is the key forensics
        property: subscriber-loop and WAL-contention diagnostics show up
        even when they originate from non-gateway loggers.
        """
        monkeypatch.delenv("HERMES_GATEWAY_LOG_FILE", raising=False)
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        # Cross-test pollution defense (mirroring test_agent_log_still_receives_all)
        for name in ("events", "tools", "gateway"):
            lg = logging.getLogger(name)
            lg.propagate = True
            lg.setLevel(logging.NOTSET)

        logging.getLogger("gateway.run").info("gateway started")
        logging.getLogger("events.bus").info("event published")
        logging.getLogger("tools.terminal_tool").info("running command")

        # Flushing root's handlers is not enough — the file handlers sit on the
        # async QueueListener; draining the queue is what gets records to disk.
        hermes_logging.flush_log_queue()

        forensics = hermes_home / "logs" / "gateway-forensics.log"
        assert forensics.exists()
        content = forensics.read_text()
        assert "gateway started" in content
        assert "event published" in content
        assert "running command" in content

    def test_HERMES_GATEWAY_LOG_FILE_overrides_path(self, hermes_home, tmp_path, monkeypatch):
        custom_path = tmp_path / "subdir" / "custom-forensics.log"
        monkeypatch.setenv("HERMES_GATEWAY_LOG_FILE", str(custom_path))

        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        root = logging.getLogger()

        custom_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).resolve() == custom_path.resolve()
        ]
        assert len(custom_handlers) == 1, (
            f"expected handler at {custom_path}, got: "
            f"{[getattr(h, 'baseFilename', '') for h in hermes_logging.rotating_file_handlers()]}"
        )

        # Default path is NOT used when override is set.
        default_path = hermes_home / "logs" / "gateway-forensics.log"
        default_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).resolve() == default_path.resolve()
        ]
        assert len(default_handlers) == 0

        # Parent directory is created on demand.
        assert custom_path.parent.is_dir()

    def test_HERMES_GATEWAY_LOG_FILE_expands_user(self, hermes_home, tmp_path, monkeypatch):
        """``~`` in the env var expands to the user's home (per Path.expanduser)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HERMES_GATEWAY_LOG_FILE", "~/my-forensics.log")

        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        root = logging.getLogger()

        expected = (tmp_path / "my-forensics.log").resolve()
        matching = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).resolve() == expected
        ]
        assert len(matching) == 1, (
            f"expected handler at {expected}, got: "
            f"{[getattr(h, 'baseFilename', '') for h in hermes_logging.rotating_file_handlers()]}"
        )

    def test_forensics_handler_idempotent_on_repeat_call(self, hermes_home, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_LOG_FILE", raising=False)
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        root = logging.getLogger()
        forensics_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "gateway-forensics.log"
        ]
        assert len(forensics_handlers) == 1

    def test_forensics_handler_added_after_cli_init(self, hermes_home, monkeypatch):
        """The forensics handler MUST be attached even when cli-mode init
        ran first (the production path — see TestGatewayMode regression note).
        """
        monkeypatch.delenv("HERMES_GATEWAY_LOG_FILE", raising=False)
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="cli")
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        root = logging.getLogger()
        forensics_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "gateway-forensics.log"
        ]
        assert len(forensics_handlers) == 1

    def test_invalid_HERMES_GATEWAY_LOG_FILE_does_not_crash(
        self, hermes_home, tmp_path, monkeypatch, caplog
    ):
        """Bad ``HERMES_GATEWAY_LOG_FILE`` must not crash the gateway —
        forensics is best-effort. The curated gateway.log handler stays
        attached and the failure is surfaced via a warning.
        """
        # Create a file where a directory is expected. _add_rotating_handler
        # will then fail at path.parent.mkdir() with NotADirectoryError on
        # both POSIX and Windows.
        blocker = tmp_path / "blocker-file"
        blocker.write_text("not a directory")
        monkeypatch.setenv(
            "HERMES_GATEWAY_LOG_FILE", str(blocker / "child" / "forensics.log")
        )

        with caplog.at_level(logging.WARNING, logger="hermes_logging"):
            # Must not raise.
            hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        # Curated gateway.log handler stays attached even when forensics fails.
        root = logging.getLogger()
        gw_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "gateway.log"
        ]
        assert len(gw_handlers) == 1

        # Warning was emitted naming the env var.
        assert any(
            "gateway-forensics handler failed to attach" in r.message
            for r in caplog.records
        ), f"expected warning, got: {[r.message for r in caplog.records]}"


class TestGuiMode:
    """setup_logging(mode='gui') creates a filtered gui.log."""

    def test_gui_log_created(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gui")
        root = logging.getLogger()

        gui_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and "gui.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gui_handlers) == 1

    def test_gui_log_created_after_cli_init(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="cli")
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gui")

        root = logging.getLogger()
        gui_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and "gui.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gui_handlers) == 1

    def test_gui_log_receives_only_gui_components(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gui")

        logging.getLogger("hermes_cli.web_server").info("dashboard online")
        logging.getLogger("tui_gateway.ws").info("ws connected")
        logging.getLogger("gateway.run").info("gateway event")

        hermes_logging.flush_log_queue()

        gui_log = hermes_home / "logs" / "gui.log"
        assert gui_log.exists()
        content = gui_log.read_text()
        assert "dashboard online" in content
        assert "ws connected" in content
        assert "gateway event" not in content


class TestSessionContext:
    """set_session_context / clear_session_context + _SessionFilter."""

    def test_session_tag_in_log_output(self, hermes_home):
        """When session context is set, log lines include [session_id]."""
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.set_session_context("abc123")

        test_logger = logging.getLogger("test.session_tag")
        test_logger.info("tagged message")

        hermes_logging.flush_log_queue()

        agent_log = hermes_home / "logs" / "agent.log"
        content = agent_log.read_text()
        assert "[abc123]" in content
        assert "tagged message" in content

    def test_no_session_tag_without_context(self, hermes_home):
        """Without session context, log lines have no session tag."""
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.clear_session_context()

        test_logger = logging.getLogger("test.no_session")
        test_logger.info("untagged message")

        hermes_logging.flush_log_queue()

        agent_log = hermes_home / "logs" / "agent.log"
        content = agent_log.read_text()
        assert "untagged message" in content
        # Should not have any [xxx] session tag
        import re
        for line in content.splitlines():
            if "untagged message" in line:
                assert not re.search(r"\[.+?\]", line.split("INFO")[1].split("test.no_session")[0])

    def test_clear_session_context(self, hermes_home):
        """After clearing, session tag disappears."""
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.set_session_context("xyz789")
        hermes_logging.clear_session_context()

        test_logger = logging.getLogger("test.cleared")
        test_logger.info("after clear")

        hermes_logging.flush_log_queue()

        agent_log = hermes_home / "logs" / "agent.log"
        content = agent_log.read_text()
        assert "[xyz789]" not in content

    def test_session_context_thread_isolated(self, hermes_home):
        """Session context is per-thread — one thread's context doesn't leak."""
        hermes_logging.setup_logging(hermes_home=hermes_home)

        results = {}

        def thread_a():
            hermes_logging.set_session_context("thread_a_session")
            logging.getLogger("test.thread_a").info("from thread A")
            hermes_logging.flush_log_queue()

        def thread_b():
            hermes_logging.set_session_context("thread_b_session")
            logging.getLogger("test.thread_b").info("from thread B")
            hermes_logging.flush_log_queue()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        ta.join()
        tb.start()
        tb.join()

        agent_log = hermes_home / "logs" / "agent.log"
        content = agent_log.read_text()

        # Each thread's message should have its own session tag
        for line in content.splitlines():
            if "from thread A" in line:
                assert "[thread_a_session]" in line
                assert "[thread_b_session]" not in line
            if "from thread B" in line:
                assert "[thread_b_session]" in line
                assert "[thread_a_session]" not in line


class TestRecordFactory:
    """Unit tests for the custom LogRecord factory."""

    def test_record_has_session_tag(self):
        """Every record gets a session_tag attribute."""
        factory = logging.getLogRecordFactory()
        record = factory("test", logging.INFO, "", 0, "msg", (), None)
        assert hasattr(record, "session_tag")

    def test_empty_tag_without_context(self):
        hermes_logging.clear_session_context()
        factory = logging.getLogRecordFactory()
        record = factory("test", logging.INFO, "", 0, "msg", (), None)
        assert record.session_tag == ""

    def test_tag_with_context(self):
        hermes_logging.set_session_context("sess_42")
        factory = logging.getLogRecordFactory()
        record = factory("test", logging.INFO, "", 0, "msg", (), None)
        assert record.session_tag == " [sess_42]"

    def test_idempotent_install(self):
        """Calling _install_session_record_factory() twice doesn't double-wrap."""
        hermes_logging._install_session_record_factory()
        factory_a = logging.getLogRecordFactory()
        hermes_logging._install_session_record_factory()
        factory_b = logging.getLogRecordFactory()
        assert factory_a is factory_b

    def test_works_with_any_handler(self):
        """A handler using %(session_tag)s works even without _SessionFilter."""
        hermes_logging.set_session_context("any_handler_test")
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(session_tag)s %(message)s"))

        logger = logging.getLogger("_test_any_handler")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            # Should not raise KeyError
            logger.info("hello")
        finally:
            logger.removeHandler(handler)


class TestComponentFilter:
    """Unit tests for _ComponentFilter."""

    def test_passes_matching_prefix(self):
        f = hermes_logging._ComponentFilter(("gateway",))
        record = logging.LogRecord(
            "gateway.run", logging.INFO, "", 0, "msg", (), None
        )
        assert f.filter(record) is True

    def test_passes_nested_matching_prefix(self):
        # Migrated platform adapters log under plugins.platforms.* (#41112);
        # the gateway component filter is built from COMPONENT_PREFIXES["gateway"]
        # (which includes "plugins.platforms"), so such records pass.
        f = hermes_logging._ComponentFilter(
            hermes_logging.COMPONENT_PREFIXES["gateway"]
        )
        record = logging.LogRecord(
            "plugins.platforms.telegram.adapter", logging.INFO, "", 0, "msg", (), None
        )
        assert f.filter(record) is True

    def test_blocks_non_matching(self):
        f = hermes_logging._ComponentFilter(("gateway",))
        record = logging.LogRecord(
            "tools.terminal_tool", logging.INFO, "", 0, "msg", (), None
        )
        assert f.filter(record) is False

    def test_multiple_prefixes(self):
        f = hermes_logging._ComponentFilter(("agent", "run_agent", "model_tools"))
        assert f.filter(logging.LogRecord(
            "agent.compressor", logging.INFO, "", 0, "", (), None
        ))
        assert f.filter(logging.LogRecord(
            "run_agent", logging.INFO, "", 0, "", (), None
        ))
        assert f.filter(logging.LogRecord(
            "model_tools", logging.INFO, "", 0, "", (), None
        ))
        assert not f.filter(logging.LogRecord(
            "tools.browser", logging.INFO, "", 0, "", (), None
        ))


class TestComponentPrefixes:
    """COMPONENT_PREFIXES covers the expected components."""

    def test_gateway_prefix(self):
        assert "gateway" in hermes_logging.COMPONENT_PREFIXES
        # The gateway component captures core gateway logs, the hermes_plugins
        # facility, and plugins.platforms (messaging-platform adapters that
        # migrated out of gateway/platforms/ into bundled plugins, #41112).
        # Assert the required members as an invariant rather than an exact
        # tuple snapshot so adding future gateway-component prefixes doesn't
        # break this test.
        gateway_prefixes = hermes_logging.COMPONENT_PREFIXES["gateway"]
        assert "gateway" in gateway_prefixes
        assert "hermes_plugins" in gateway_prefixes
        assert "plugins.platforms" in gateway_prefixes

    def test_agent_prefix(self):
        prefixes = hermes_logging.COMPONENT_PREFIXES["agent"]
        assert "agent" in prefixes
        assert "run_agent" in prefixes
        assert "model_tools" in prefixes

    def test_tools_prefix(self):
        assert ("tools",) == hermes_logging.COMPONENT_PREFIXES["tools"]

    def test_cli_prefix(self):
        prefixes = hermes_logging.COMPONENT_PREFIXES["cli"]
        assert "hermes_cli" in prefixes
        assert "cli" in prefixes

    def test_cron_prefix(self):
        assert ("cron",) == hermes_logging.COMPONENT_PREFIXES["cron"]

    def test_gui_prefix(self):
        prefixes = hermes_logging.COMPONENT_PREFIXES["gui"]
        assert "hermes_cli.web_server" in prefixes
        assert "tui_gateway" in prefixes


class TestSetupVerboseLogging:
    """setup_verbose_logging() adds a DEBUG-level console handler."""

    def test_adds_stream_handler(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_verbose_logging()

        root = logging.getLogger()
        verbose_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
            and getattr(h, "_hermes_verbose", False)
        ]
        assert len(verbose_handlers) == 1
        assert verbose_handlers[0].level == logging.DEBUG

    def test_idempotent(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_verbose_logging()
        hermes_logging.setup_verbose_logging()  # second call

        root = logging.getLogger()
        verbose_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
            and getattr(h, "_hermes_verbose", False)
        ]
        assert len(verbose_handlers) == 1


class TestAddRotatingHandler:
    """_add_rotating_handler() is idempotent and creates the directory."""

    def test_creates_directory(self, tmp_path):
        log_path = tmp_path / "subdir" / "test.log"
        logger = logging.getLogger("_test_rotating")
        formatter = logging.Formatter("%(message)s")

        hermes_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )

        assert log_path.parent.is_dir()
        # Clean up
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    def test_no_duplicate_for_same_path(self, tmp_path):
        log_path = tmp_path / "test.log"
        logger = logging.getLogger("_test_rotating_dup")
        formatter = logging.Formatter("%(message)s")

        hermes_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )
        hermes_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )

        rotating_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
        ]
        assert len(rotating_handlers) == 1
        # Clean up
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    def test_log_filter_attached(self, tmp_path):
        """Optional log_filter is attached to the handler."""
        log_path = tmp_path / "filtered.log"
        logger = logging.getLogger("_test_rotating_filter")
        formatter = logging.Formatter("%(message)s")
        component_filter = hermes_logging._ComponentFilter(("test",))

        hermes_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
            log_filter=component_filter,
        )

        handlers = [h for h in hermes_logging.rotating_file_handlers() if isinstance(h, RotatingFileHandler)]
        assert len(handlers) == 1
        assert component_filter in handlers[0].filters
        # Clean up
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    def test_no_session_filter_on_handler(self, tmp_path):
        """Handlers rely on record factory, not per-handler _SessionFilter."""
        log_path = tmp_path / "no_session_filter.log"
        logger = logging.getLogger("_test_no_session_filter")
        formatter = logging.Formatter("%(session_tag)s%(message)s")

        hermes_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )

        handlers = [h for h in hermes_logging.rotating_file_handlers() if isinstance(h, RotatingFileHandler)]
        assert len(handlers) == 1
        # No _SessionFilter on the handler — record factory handles it
        assert len(handlers[0].filters) == 0

        # But session_tag still works (via record factory)
        hermes_logging.set_session_context("factory_test")
        logger.info("test msg")
        hermes_logging.flush_log_queue()
        content = log_path.read_text()
        assert "[factory_test]" in content

        # Clean up
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    @pytest.mark.skipif(
        os.name == "nt",
        reason=(
            "POSIX-only guarantee. Managed mode is NixOS-only (is_managed()), and "
            "the guarantee under test — chmod 0660 so the hermes group can share "
            "the log — has no Windows equivalent: os.chmod() there only toggles "
            "the read-only bit, so S_IMODE is always 0o666/0o444. The Windows "
            "handler is concurrent-log-handler (#44873), whose _open() returns "
            "None and creates the file lazily in do_write(), so neither the "
            "eager creation nor the chmod hook this test exercises applies."
        ),
    )
    def test_managed_mode_initial_open_sets_group_writable(self, tmp_path):
        log_path = tmp_path / "managed-open.log"
        logger = logging.getLogger("_test_rotating_managed_open")
        formatter = logging.Formatter("%(message)s")

        old_umask = os.umask(0o022)
        try:
            with patch("hermes_cli.config.is_managed", return_value=True):
                hermes_logging._add_rotating_handler(
                    logger, log_path,
                    level=logging.INFO, max_bytes=1024, backup_count=1,
                    formatter=formatter,
                )
        finally:
            os.umask(old_umask)

        assert log_path.exists()
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o660

        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    @pytest.mark.skipif(
        os.name == "nt",
        reason=(
            "POSIX-only guarantee — see "
            "test_managed_mode_initial_open_sets_group_writable. chmod 0660 is "
            "meaningless on Windows (S_IMODE is 0o666 here) and managed mode is "
            "NixOS-only."
        ),
    )
    def test_managed_mode_rollover_sets_group_writable(self, tmp_path):
        log_path = tmp_path / "managed-rollover.log"
        logger = logging.getLogger("_test_rotating_managed_rollover")
        formatter = logging.Formatter("%(message)s")

        old_umask = os.umask(0o022)
        try:
            with patch("hermes_cli.config.is_managed", return_value=True):
                hermes_logging._add_rotating_handler(
                    logger, log_path,
                    level=logging.INFO, max_bytes=1, backup_count=1,
                    formatter=formatter,
                )
                handler = next(
                    h for h in hermes_logging.rotating_file_handlers() if isinstance(h, RotatingFileHandler)
                )
                logger.info("a" * 256)
                hermes_logging.flush_log_queue()
        finally:
            os.umask(old_umask)

        assert log_path.exists()
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o660

        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()


class TestWindowsConcurrentLogLockTimeout:
    """Windows concurrent-log-handler lock timeouts stay inside logging."""

    def _make_logger_and_handler(self, log_path: Path):
        logger = logging.getLogger(f"_test_concurrent_lock_timeout_{log_path.stem}")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)

        handler = hermes_logging._ManagedRotatingFileHandler(
            str(log_path), maxBytes=1, backupCount=1, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        return logger, handler

    def test_helper_only_matches_windows_concurrent_lock_timeout(self):
        with patch.object(hermes_logging.sys, "platform", "win32"):
            assert hermes_logging._is_windows_concurrent_log_lock_timeout(
                RuntimeError("Cannot acquire lock after 20 attempts")
            )
            assert not hermes_logging._is_windows_concurrent_log_lock_timeout(
                RuntimeError("some other logging failure")
            )

        with patch.object(hermes_logging.sys, "platform", "linux"):
            assert not hermes_logging._is_windows_concurrent_log_lock_timeout(
                RuntimeError("Cannot acquire lock after 20 attempts")
            )

    def test_lock_timeout_routed_to_handle_error_is_suppressed(self, tmp_path, capsys):
        """Mirror CLH's real control flow.

        ``concurrent-log-handler``'s ``emit()`` wraps its whole body in
        ``try/except Exception: self.handleError(record)``, so the lock
        RuntimeError raised in ``_do_lock()`` is caught *inside* CLH and routed
        to ``handleError`` with the exception live in ``sys.exc_info()``.  We
        invoke ``handleError`` the same way CLH would and assert no traceback
        reaches stderr (the slash-worker surface)."""
        logger, handler = self._make_logger_and_handler(tmp_path / "agent.log")
        record = logger.makeRecord(
            logger.name, logging.INFO, __file__, 0, "force rollover", (), None,
        )
        try:
            with patch.object(hermes_logging.sys, "platform", "win32"):
                try:
                    raise RuntimeError("Cannot acquire lock after 20 attempts")
                except RuntimeError:
                    handler.handleError(record)

            captured = capsys.readouterr()
            assert "Cannot acquire lock after 20 attempts" not in captured.err
            assert "--- Logging error ---" not in captured.err
        finally:
            logger.removeHandler(handler)
            handler.close()

    def test_other_errors_routed_to_handle_error_still_print(self, tmp_path, capsys):
        """An unrelated failure routed through ``handleError`` must still emit the
        normal stdlib logging-error output — only the known CLH timeout is silent."""
        logger, handler = self._make_logger_and_handler(tmp_path / "agent.log")
        record = logger.makeRecord(
            logger.name, logging.INFO, __file__, 0, "force rollover", (), None,
        )
        try:
            with patch.object(hermes_logging.sys, "platform", "win32"):
                try:
                    raise RuntimeError("unexpected logging failure")
                except RuntimeError:
                    handler.handleError(record)

            captured = capsys.readouterr()
            assert "unexpected logging failure" in captured.err
            assert "--- Logging error ---" in captured.err
        finally:
            logger.removeHandler(handler)
            handler.close()


class TestReadLoggingConfig:
    """_read_logging_config() reads from config.yaml."""

    def test_returns_none_when_no_config(self, hermes_home):
        level, max_size, backup = hermes_logging._read_logging_config()
        assert level is None
        assert max_size is None
        assert backup is None

    def test_reads_logging_section(self, hermes_home):
        import yaml
        config = {"logging": {"level": "DEBUG", "max_size_mb": 10, "backup_count": 5}}
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        level, max_size, backup = hermes_logging._read_logging_config()
        assert level == "DEBUG"
        assert max_size == 10
        assert backup == 5

    def test_handles_missing_logging_section(self, hermes_home):
        import yaml
        config = {"model": "test"}
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        level, max_size, backup = hermes_logging._read_logging_config()
        assert level is None


class TestInferDaemonRole:
    """infer_daemon_role() maps a process's argv to a daemon role or None."""

    def test_gateway_subcommand(self):
        assert hermes_logging.infer_daemon_role(["hermes", "gateway", "run"]) == "gateway"

    def test_dashboard_subcommand(self):
        assert hermes_logging.infer_daemon_role(["hermes", "dashboard"]) == "dashboard"

    def test_proxy_subcommand(self):
        assert hermes_logging.infer_daemon_role(
            ["hermes", "proxy", "start", "--provider", "nous"]
        ) == "proxy"

    def test_global_flags_before_subcommand(self):
        assert hermes_logging.infer_daemon_role(
            ["hermes", "--profile", "main", "gateway", "run"]
        ) == "gateway"

    def test_devflow_bridge_runner_by_argv0(self):
        assert hermes_logging.infer_daemon_role(
            ["/x/profiles/main/scripts/devflow_bridge_runner.py"]
        ) == "devflow-bridge"

    def test_transient_chat_is_none(self):
        assert hermes_logging.infer_daemon_role(["hermes", "chat"]) is None

    def test_logs_gateway_is_not_gateway_daemon(self):
        assert hermes_logging.infer_daemon_role(["hermes", "logs", "gateway"]) is None

    def test_empty_argv_is_none(self):
        assert hermes_logging.infer_daemon_role([]) is None

    def test_defaults_to_sys_argv(self, monkeypatch):
        monkeypatch.setattr(hermes_logging.sys, "argv", ["hermes", "dashboard"])
        assert hermes_logging.infer_daemon_role() == "dashboard"

    def test_short_profile_flag_before_subcommand(self):
        """`-p default dashboard` is the canonical monitor/laptop-start launch
        line. The short flag consumes its value, so the subcommand is
        ``dashboard`` — not ``default``. Before this was handled the role came
        back None and every monitor-launched dashboard logged to the shared
        agent.log instead of agent-dashboard.log."""
        assert hermes_logging.infer_daemon_role(
            ["hermes", "-p", "default", "dashboard", "--port", "9119"]
        ) == "dashboard"

    def test_short_valueless_flag_does_not_eat_subcommand(self):
        """Only value-taking short flags consume the next token. A bare
        toggle like ``-v`` must not swallow the subcommand."""
        assert hermes_logging.infer_daemon_role(
            ["hermes", "-v", "gateway", "run"]
        ) == "gateway"

    def test_short_profile_flag_with_inline_value(self):
        assert hermes_logging.infer_daemon_role(
            ["hermes", "-p=default", "dashboard"]
        ) == "dashboard"


class TestInferLogMode:
    """infer_log_mode() picks the gui log mode for dashboard-family entrypoints."""

    def test_dashboard_is_gui(self):
        assert hermes_logging.infer_log_mode(["hermes", "dashboard"]) == "gui"

    def test_short_profile_flag_before_dashboard_is_gui(self):
        """Same short-flag trap as infer_daemon_role: the canonical
        `-p default dashboard` launch was resolving to 'cli', so the process
        never attached a gui.log handler."""
        assert hermes_logging.infer_log_mode(
            ["hermes", "-p", "default", "dashboard", "--port", "9119"]
        ) == "gui"

    def test_serve_is_gui(self):
        assert hermes_logging.infer_log_mode(
            ["hermes", "--profile", "main", "serve"]
        ) == "gui"

    def test_chat_is_cli(self):
        assert hermes_logging.infer_log_mode(["hermes", "chat"]) == "cli"

    def test_empty_argv_is_cli(self):
        assert hermes_logging.infer_log_mode([]) == "cli"


class TestRoleScopedCatchAll:
    """role= routes the catch-all logs to per-role filenames."""

    def _rotating(self, name):
        # File handlers live behind the async QueueListener, not on the root
        # logger — root only carries the _NonFormattingQueueHandler.
        return [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == name
        ]

    def test_transient_uses_shared_agent_log(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)  # role=None
        assert len(self._rotating("agent.log")) == 1
        assert len(self._rotating("errors.log")) == 1

    def test_role_uses_per_role_files(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, role="dashboard")
        assert len(self._rotating("agent-dashboard.log")) == 1
        assert len(self._rotating("errors-dashboard.log")) == 1
        assert len(self._rotating("agent.log")) == 0
        assert len(self._rotating("errors.log")) == 0

    def test_gateway_mode_defaults_role_to_gateway(self, hermes_home, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_LOG_FILE", raising=False)
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        assert len(self._rotating("agent-gateway.log")) == 1
        assert len(self._rotating("errors-gateway.log")) == 1
        assert len(self._rotating("agent.log")) == 0
        assert len(self._rotating("gateway.log")) == 1

    def test_explicit_role_overrides_gateway_mode_default(self, hermes_home, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_LOG_FILE", raising=False)
        hermes_logging.setup_logging(
            hermes_home=hermes_home, mode="gateway", role="proxy"
        )
        # Explicit role wins over the mode="gateway" default.
        assert len(self._rotating("agent-proxy.log")) == 1
        assert len(self._rotating("agent-gateway.log")) == 0

    def test_role_catch_all_actually_writes(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, role="proxy")
        logging.getLogger("test.proxy_role").info("proxy role line")
        hermes_logging.flush_log_queue()
        agent_proxy = hermes_home / "logs" / "agent-proxy.log"
        assert agent_proxy.exists()
        assert "proxy role line" in agent_proxy.read_text()


class TestLogsKnownFiles:
    """hermes_cli.logs.LOG_FILES exposes the per-role daemon catch-all files."""

    def test_role_files_registered(self):
        from hermes_cli.logs import LOG_FILES
        assert LOG_FILES["agent-gateway"] == "agent-gateway.log"
        assert LOG_FILES["agent-dashboard"] == "agent-dashboard.log"
        assert LOG_FILES["agent-proxy"] == "agent-proxy.log"
        assert LOG_FILES["agent-devflow-bridge"] == "agent-devflow-bridge.log"

    def test_default_agent_still_present(self):
        from hermes_cli.logs import LOG_FILES
        assert LOG_FILES["agent"] == "agent.log"


class TestWindowsSafeRollover:
    """_ManagedRotatingFileHandler tolerates a Windows file lock at rollover.

    Regression for the gateway log-rotation bug: a sibling Hermes process (or
    a log reader) holding agent.log open made os.rename fail with
    PermissionError [WinError 32]. The stdlib handler then raised on every
    emit (traceback storm to stderr) AND destroyed the backup chain because
    it shuffles/removes .1/.2 BEFORE the failing base→.1 rename.
    """

    def _make_handler(self, tmp_path, monkeypatch, **kwargs):
        # No real sleeping between retries — keep the suite fast.
        monkeypatch.setattr(hermes_logging, "_ROLLOVER_RETRY_DELAY_SEC", 0)
        base = tmp_path / "agent.log"
        handler = hermes_logging._ManagedRotatingFileHandler(
            str(base), maxBytes=kwargs.get("max_bytes", 200),
            backupCount=kwargs.get("backup_count", 3), encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        return base, handler

    def test_locked_rollover_is_nonfatal_and_preserves_backups(self, tmp_path, monkeypatch):
        base, handler = self._make_handler(tmp_path, monkeypatch)
        try:
            # Pre-existing rotated history that must NOT be destroyed.
            (tmp_path / "agent.log.1").write_text("BACKUP-1", encoding="utf-8")
            (tmp_path / "agent.log.2").write_text("BACKUP-2", encoding="utf-8")
            handler.emit(logging.makeLogRecord({"msg": "live-content"}))

            # Simulate another process holding the file: every os.replace
            # raises, exactly like WinError 32 on a locked rename.
            def _boom(src, dst, *a, **k):
                raise PermissionError(32, "The process cannot access the file")
            monkeypatch.setattr(os, "replace", _boom)

            # MUST NOT raise (stdlib would, dumping a traceback per emit).
            handler.doRollover()

            # Backups untouched — the bug erased these.
            assert (tmp_path / "agent.log.1").read_text(encoding="utf-8") == "BACKUP-1"
            assert (tmp_path / "agent.log.2").read_text(encoding="utf-8") == "BACKUP-2"
            # Logging keeps working through the lock.
            _assert_still_logging(handler, base, "after-lock")
            # Rotation is deferred: shouldRollover stays quiet during cooldown,
            # so the per-emit traceback storm cannot happen.
            big = logging.makeLogRecord({"msg": "x" * 1000})
            assert handler.shouldRollover(big) is False
        finally:
            handler.close()

    def test_rollover_succeeds_and_shifts_backups_when_unlocked(self, tmp_path, monkeypatch):
        base, handler = self._make_handler(tmp_path, monkeypatch)
        try:
            (tmp_path / "agent.log.1").write_text("OLD-1", encoding="utf-8")
            handler.emit(logging.makeLogRecord({"msg": "current-line"}))

            handler.doRollover()

            # Base content rotated into .1 …
            assert "current-line" in (tmp_path / "agent.log.1").read_text(encoding="utf-8")
            # … and the old .1 shifted to .2.
            assert (tmp_path / "agent.log.2").read_text(encoding="utf-8") == "OLD-1"
            # Fresh base that the handler can still write to.
            _assert_still_logging(handler, base, "post-rollover")
            assert handler._rollover_blocked_until == 0.0
        finally:
            handler.close()

    def test_recovers_after_lock_clears(self, tmp_path, monkeypatch):
        base, handler = self._make_handler(tmp_path, monkeypatch)
        try:
            handler.emit(logging.makeLogRecord({"msg": "first"}))

            def _boom(src, dst, *a, **k):
                raise PermissionError(32, "in use")
            monkeypatch.setattr(os, "replace", _boom)
            handler.doRollover()  # deferred
            assert handler._rollover_blocked_until > 0.0

            # Lock clears: restore real os.replace and bypass the cooldown
            # (as time would do after _ROLLOVER_COOLDOWN_SEC).
            monkeypatch.undo()
            monkeypatch.setattr(hermes_logging, "_ROLLOVER_RETRY_DELAY_SEC", 0)
            handler._rollover_blocked_until = 0.0
            handler.emit(logging.makeLogRecord({"msg": "second"}))

            handler.doRollover()
            assert (tmp_path / "agent.log.1").exists()
            assert handler._rollover_blocked_until == 0.0
        finally:
            handler.close()

    @pytest.mark.skipif(os.name != "nt", reason="Windows-only file-lock semantics")
    def test_real_windows_lock_is_nonfatal(self, tmp_path, monkeypatch):
        """End-to-end with a REAL second handle (no mocks).

        On Windows, a second open handle on the base file makes the real
        os.replace fail with PermissionError [WinError 32] — exactly the
        gateway failure. Asserts the handler survives it without mocks.
        """
        base, handler = self._make_handler(tmp_path, monkeypatch)
        try:
            (tmp_path / "agent.log.1").write_text("KEEP-1", encoding="utf-8")
            handler.emit(logging.makeLogRecord({"msg": "live"}))

            # A sibling process holding agent.log open == another open handle.
            with open(base, "a", encoding="utf-8"):
                handler.doRollover()  # real os.replace → WinError 32

                assert (tmp_path / "agent.log.1").read_text(encoding="utf-8") == "KEEP-1"
                assert handler._rollover_blocked_until > 0.0
                _assert_still_logging(handler, base, "through-lock")

            # Lock released → rotation resumes.
            handler._rollover_blocked_until = 0.0
            handler.emit(logging.makeLogRecord({"msg": "after"}))
            handler.doRollover()
            assert "live" in (tmp_path / "agent.log.1").read_text(encoding="utf-8")
            assert handler._rollover_blocked_until == 0.0
        finally:
            handler.close()


class TestPerRoleRotationIsolation:
    """A daemon's per-role catch-all rotates even while the shared agent.log
    is held open by another process — the regression the per-role split fixes.
    """

    def _role_handler(self, tmp_path, monkeypatch, role):
        monkeypatch.setattr(hermes_logging, "_ROLLOVER_RETRY_DELAY_SEC", 0)
        base = tmp_path / f"agent-{role}.log"
        handler = hermes_logging._ManagedRotatingFileHandler(
            str(base), maxBytes=200, backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        return base, handler

    def test_role_file_rotates_crossplatform(self, tmp_path, monkeypatch):
        """Sanity: with no lock at all, the per-role file rotates normally."""
        base, handler = self._role_handler(tmp_path, monkeypatch, "dashboard")
        try:
            (tmp_path / "agent-dashboard.log.1").write_text("OLD-1", encoding="utf-8")
            handler.emit(logging.makeLogRecord({"msg": "current"}))
            handler.doRollover()
            assert "current" in (tmp_path / "agent-dashboard.log.1").read_text(encoding="utf-8")
            assert (tmp_path / "agent-dashboard.log.2").read_text(encoding="utf-8") == "OLD-1"
            _assert_still_logging(handler, base, "post-rollover")
            assert handler._rollover_blocked_until == 0.0
        finally:
            handler.close()

    @pytest.mark.skipif(os.name != "nt", reason="Windows-only file-lock semantics")
    def test_role_file_rotates_while_shared_agent_log_is_pinned(self, tmp_path, monkeypatch):
        """The acceptance scenario: another process pins the SHARED agent.log;
        the daemon's private agent-<role>.log must still rotate cleanly
        (different file => no cross-process lock).
        """
        base, handler = self._role_handler(tmp_path, monkeypatch, "dashboard")
        shared = tmp_path / "agent.log"
        shared.write_text("shared-held-open", encoding="utf-8")
        try:
            handler.emit(logging.makeLogRecord({"msg": "role-line"}))
            with open(shared, "a", encoding="utf-8"):
                handler.doRollover()
                assert "role-line" in (tmp_path / "agent-dashboard.log.1").read_text(encoding="utf-8")
                assert handler._rollover_blocked_until == 0.0  # NOT deferred
                _assert_still_logging(handler, base, "post-rollover")
        finally:
            handler.close()


class TestExternalRotationRecovery:
    """_ManagedRotatingFileHandler recovers from external rotation.

    External rotation = anything that renames, unlinks, or replaces the
    log file without going through ``doRollover()``: logrotate, manual
    ``mv``, another process rotating under us, or a transient ``rm``.
    Before this fix the open file descriptor stayed pinned to the old
    inode forever, so every subsequent write went to the rotated backup
    instead of the file the operator expects to read.
    """

    def _make_handler(self, log_path: Path) -> hermes_logging._ManagedRotatingFileHandler:
        handler = hermes_logging._ManagedRotatingFileHandler(
            str(log_path), maxBytes=10 * 1024 * 1024, backupCount=3,
            encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    def _emit(self, handler: logging.Handler, msg: str) -> None:
        record = logging.LogRecord(
            name="gateway.run", level=logging.INFO, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )
        # Match the record factory that hermes_logging installs at import time.
        record.session_tag = ""
        handler.emit(record)
        hermes_logging.flush_log_queue()

    def test_recovers_after_external_rename(self, tmp_path):
        """logrotate-style external rename: ``mv gateway.log gateway.log.1``.

        Handler's fd was pinned to the renamed inode; new writes used to
        go to ``gateway.log.1`` forever.  After fix, the handler reopens
        ``gateway.log`` at the original path.
        """
        log_path = tmp_path / "gateway.log"
        rotated = tmp_path / "gateway.log.1"
        handler = self._make_handler(log_path)
        try:
            self._emit(handler, "before rotation")
            assert log_path.read_text() == "before rotation\n"

            # External rotation (NOT via handler.doRollover()).
            os.rename(log_path, rotated)
            assert not log_path.exists()

            self._emit(handler, "after rotation")

            # The new write should land in a freshly recreated gateway.log,
            # not appended to the rotated backup.
            assert log_path.exists(), "handler did not recreate gateway.log"
            assert log_path.read_text() == "after rotation\n"
            assert rotated.read_text() == "before rotation\n"
        finally:
            handler.close()

    def test_recovers_after_external_unlink(self, tmp_path):
        """``rm gateway.log`` then keep writing — handler recreates the file."""
        log_path = tmp_path / "gateway.log"
        handler = self._make_handler(log_path)
        try:
            self._emit(handler, "before unlink")
            assert log_path.read_text() == "before unlink\n"

            os.unlink(log_path)
            assert not log_path.exists()

            self._emit(handler, "after unlink")
            assert log_path.exists()
            assert log_path.read_text() == "after unlink\n"
        finally:
            handler.close()

    def test_external_truncate_does_not_force_reopen(self, tmp_path):
        """``: > gateway.log`` keeps the same inode — no reopen needed.

        Truncation in place preserves the inode, so subsequent writes
        continue to the same file descriptor.  We assert the post-truncate
        content reflects the truncate (size shrinks) and then grows with
        new writes — i.e. the handler correctly does NOT detect this as
        an inode change.
        """
        log_path = tmp_path / "gateway.log"
        handler = self._make_handler(log_path)
        try:
            self._emit(handler, "AAAA" * 32)
            assert log_path.stat().st_size > 0

            with open(log_path, "w"):
                pass  # truncate to zero
            assert log_path.stat().st_size == 0

            self._emit(handler, "after truncate")
            assert log_path.read_text() == "after truncate\n"
        finally:
            handler.close()

    def test_normal_rollover_still_works(self, tmp_path):
        """Handler-driven ``doRollover()`` must continue to work normally.

        Regression guard: the inode-snapshot bookkeeping must be refreshed
        in ``doRollover()`` so the very next emit doesn't mistake our own
        rollover for an external one and double-reopen.
        """
        log_path = tmp_path / "gateway.log"
        rotated = tmp_path / "gateway.log.1"

        # Tiny maxBytes forces rollover after the first record.
        handler = hermes_logging._ManagedRotatingFileHandler(
            str(log_path), maxBytes=1, backupCount=1, encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        try:
            self._emit(handler, "first record")
            self._emit(handler, "second record")
            self._emit(handler, "third record")

            # After rollover we should have BOTH files, with the most
            # recent record in the live file.
            assert log_path.exists()
            assert rotated.exists()
            assert "third record" in log_path.read_text()
        finally:
            handler.close()

    def test_gateway_log_attached_after_external_rotation_then_re_setup(
        self, hermes_home,
    ):
        """End-to-end Allen-reproduction: gateway.log gets externally rotated,
        ``setup_logging(mode='gateway')`` is re-called, the handler keeps
        working.

        Reproduces Allen's symptom (gateway.log frozen mid-write, all gateway
        records leaking to agent.log) when something external rotates the
        file between setup_logging() calls.
        """
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        gw_path = hermes_home / "logs" / "gateway.log"
        rotated = hermes_home / "logs" / "gateway.log.1"

        logging.getLogger("gateway.run").info("line BEFORE rotation")
        hermes_logging.flush_log_queue()
        assert "BEFORE rotation" in gw_path.read_text()

        # External actor renames the file out from under us.
        os.rename(gw_path, rotated)
        assert not gw_path.exists()

        # Caller (or some restart path) re-enters setup_logging.  This used
        # to silently no-op due to the per-path dedup check, leaving the
        # stale fd in place.
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        logging.getLogger("gateway.run").info("line AFTER rotation")
        hermes_logging.flush_log_queue()

        # The new record must reach the live gateway.log, not the rotated
        # backup.  Allen's logs had everything past the rotation point
        # going into agent.log only, never gateway.log.
        assert gw_path.exists(), "gateway.log was never recreated"
        assert "AFTER rotation" in gw_path.read_text()
        assert "AFTER rotation" not in rotated.read_text()


class TestSafeStderr:
    """Tests for _safe_stderr() — Unicode tolerance on Windows console."""

    def test_returns_stderr_on_utf8_system(self, monkeypatch):
        """On UTF-8 systems, _safe_stderr() returns sys.stderr unchanged."""
        fake_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", fake_stderr)
        # On Linux/macOS, encoding is typically utf-8
        result = hermes_logging._safe_stderr()
        # Should return the same object (or a equivalent stream)
        assert result is fake_stderr or getattr(result, "encoding", "").lower().startswith("utf")

    def test_wraps_non_utf8_stderr(self, monkeypatch):
        """On non-UTF-8 systems (e.g. Windows cp949), wraps stderr with UTF-8."""

        class FakeStderr:
            """Simulates a Windows stderr with legacy encoding."""
            encoding = "cp949"
            buffer = io.BytesIO()

            def write(self, s):
                pass

            def flush(self):
                pass

        fake = FakeStderr()
        monkeypatch.setattr(sys, "stderr", fake)
        result = hermes_logging._safe_stderr()
        # Should be a TextIOWrapper, not the original FakeStderr
        assert isinstance(result, io.TextIOWrapper)
        assert result.encoding == "utf-8"
        assert result.errors == "replace"

    def test_handler_emits_unicode_without_crash(self, tmp_path):
        """StreamHandler with _safe_stderr can emit Unicode messages."""

        # Create a stderr-like stream with ASCII encoding
        class AsciiStream:
            encoding = "ascii"
            buffer = io.BytesIO()

            def write(self, s):
                self.buffer.write(s.encode("ascii", errors="replace"))

            def flush(self):
                pass

        # Without the fix, this would crash on cp949/ASCII stderr.
        # With the wrapper, the em-dash is replaced with '?'
        handler = logging.StreamHandler(
            io.TextIOWrapper(
                io.BytesIO(),
                encoding="utf-8",
                errors="replace",
            )
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("_test_unicode")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            # Em-dash U+2014 — the exact character from the bug report
            logger.info("Session hygiene: 400 messages — auto-compressing")
        finally:
            logger.removeHandler(handler)


class TestAsyncQueueLogging:
    """File logging runs through a QueueListener so emits never block on the
    cross-process rotation lock (Windows event-loop-stall fix)."""

    def test_file_handlers_not_on_root(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        root = logging.getLogger()
        # Rotating file handlers live on the async listener, never on root.
        assert not any(isinstance(h, RotatingFileHandler) for h in root.handlers)
        # Exactly one queue handler funnels records to the listener.
        queue_handlers = [
            h for h in root.handlers if getattr(h, "_hermes_queue", False)
        ]
        assert len(queue_handlers) == 1
        # The real file handlers are discoverable via the accessor.
        assert any(
            "agent.log" in getattr(h, "baseFilename", "")
            for h in hermes_logging.rotating_file_handlers()
        )

    def test_records_reach_file_through_queue(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        logging.getLogger("test_async.queue").info("through the queue")
        hermes_logging.flush_log_queue()
        agent_log = hermes_home / "logs" / "agent.log"
        assert "through the queue" in agent_log.read_text()

    def test_queue_preserves_per_handler_levels(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        logging.getLogger("test_async.levels").info("info-level line")
        hermes_logging.flush_log_queue()
        errors_log = hermes_home / "logs" / "errors.log"
        # INFO must not reach the WARNING+ errors.log even through the queue.
        if errors_log.exists():
            assert "info-level line" not in errors_log.read_text()


class TestNoEagerHeavyImports:
    """Guard against ``hermes_logging`` re-acquiring a slow import graph.

    On Windows ``hermes_logging`` aliases concurrent-log-handler's
    ``ConcurrentRotatingFileHandler`` (the #44873 fix), which pulls in
    ``portalocker``.  portalocker's ``__init__`` probes for its optional
    Redis-backed lock via ``from .redis import RedisLock``; when ``redis`` is
    installed that probe SUCCEEDS and eagerly imports the entire
    ``redis`` → ``redis.observability`` → ``opentelemetry.sdk.metrics`` →
    ``psutil`` stack — ~10–14s of import cost on this box on EVERY hermes
    invocation, including ``hermes --version``.  ``hermes_logging`` blocks that
    with a ``sys.modules['portalocker.redis'] = None`` sentinel.  This test
    fails (returncode 1) if the sentinel is removed or stops working.
    """

    def test_importing_hermes_logging_stays_off_the_redis_stack(self, tmp_path):
        # Run in a clean subprocess: within the pytest process these modules
        # may already be imported by unrelated tests, which would mask the
        # regression.  A fresh interpreter isolates hermes_logging's own graph.
        code = (
            "import sys, hermes_logging; "
            "leaked = [m for m in ('redis', 'opentelemetry', 'psutil') "
            "if m in sys.modules]; "
            "print('LEAKED=' + ','.join(leaked)); "
            "sys.exit(1 if leaked else 0)"
        )
        env = dict(os.environ)
        repo_root = Path(hermes_logging.__file__).resolve().parent
        env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            # cwd pinned to tmp_path: run_tests_parallel.py gives every pytest
            # worker cwd=repo_root, so anything a child writes relative to its
            # CWD lands in the shared checkout root. Safe to repoint because
            # PYTHONPATH above already pins the import root -- and pinning it
            # is what keeps this test measuring THIS tree's hermes_logging
            # rather than the editable install's.
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, (
            "importing hermes_logging eagerly pulled a heavy import stack "
            f"(portalocker.redis sentinel regressed): {result.stdout.strip()!r} "
            f"stderr={result.stderr.strip()!r}"
        )
