"""Centralized structured logging for Hermes Agent.

Hermes writes application logs to stderr.  The default format is human-readable
text; ``logging.format: gcp_json`` switches the stream to newline-delimited JSON
using Google Cloud Logging's top-level ``severity`` and ``message`` fields.

The module intentionally does not create local log files.  Container and
service supervisors own retention, shipping, and querying of the stream.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from hermes_constants import get_config_path, get_hermes_home

_logging_initialized = False
_session_context = threading.local()
_STRUCTURED_HANDLER_ATTR = "_hermes_gcp_structured"
_TEXT_LOG_FORMAT = "%(asctime)s %(levelname)s%(session_tag)s %(name)s: %(message)s"


def _safe_stderr():  # type: ignore[return]
    """Return a stderr stream that tolerates legacy console encodings."""
    stream = sys.stderr
    encoding = getattr(stream, "encoding", None) or "utf-8"
    if encoding.lower().replace("-", "") in ("utf8", "utf8surrogateescape"):
        return stream
    try:
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            wrapped = io.TextIOWrapper(
                buf,
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
            )
            wrapped.close = lambda: None  # type: ignore[assignment]
            return wrapped
    except Exception:
        pass
    return stream


# Third-party loggers that are noisy at DEBUG/INFO level.
_NOISY_LOGGERS = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
    "asyncio",
    "hpack",
    "hpack.hpack",
    "grpc",
    "modal",
    "urllib3",
    "urllib3.connectionpool",
    "websockets",
    "charset_normalizer",
    "markdown_it",
)


# ---------------------------------------------------------------------------
# Public session context API
# ---------------------------------------------------------------------------

def set_session_context(session_id: str) -> None:
    """Set the session ID for records emitted on the current thread."""
    _session_context.session_id = session_id


def clear_session_context() -> None:
    """Clear the current thread's session context."""
    _session_context.session_id = None


def _install_session_record_factory() -> None:
    """Inject session fields into every record while preserving other factories."""
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_hermes_session_injector", False):
        return

    def _session_record_factory(*args, **kwargs):
        record = current_factory(*args, **kwargs)
        sid = getattr(_session_context, "session_id", None)
        record.session_id = sid  # type: ignore[attr-defined]
        record.session_tag = f" [{sid}]" if sid else ""  # type: ignore[attr-defined]
        return record

    _session_record_factory._hermes_session_injector = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(_session_record_factory)


_install_session_record_factory()


# ---------------------------------------------------------------------------
# Structured formatter and handler
# ---------------------------------------------------------------------------

def _cloud_severity(levelno: int) -> str:
    """Map arbitrary Python levels to Cloud Logging severities."""
    if levelno >= logging.CRITICAL:
        return "CRITICAL"
    if levelno >= logging.ERROR:
        return "ERROR"
    if levelno >= logging.WARNING:
        return "WARNING"
    if levelno >= logging.INFO:
        return "INFO"
    return "DEBUG"


class GCPStructuredLogFormatter(logging.Formatter):
    """Format a LogRecord as one Google Cloud Logging-compatible JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"

        # Keep the existing Hermes redaction policy, but only serialize safe,
        # bounded fields. Serializing record.__dict__ would expose logging extras
        # such as request arguments or provider payloads.
        try:
            from agent.redact import redact_sensitive_text

            message = redact_sensitive_text(message)
        except Exception:
            pass

        payload = {
            "time": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "severity": _cloud_severity(record.levelno),
            "message": message,
            "logger": record.name,
            "pid": os.getpid(),
            "thread": threading.get_ident(),
        }
        session_id = getattr(record, "session_id", None)
        if session_id:
            payload["session_id"] = session_id

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class GCPStructuredLogHandler(logging.StreamHandler):
    """Stream logs to stderr using a configurable text or GCP JSON formatter."""

    def __init__(self, stream=None, *, log_format: str = "gcp_json") -> None:
        super().__init__(stream if stream is not None else _safe_stderr())
        self.set_log_format(log_format)
        setattr(self, _STRUCTURED_HANDLER_ATTR, True)

    def set_log_format(self, log_format: str) -> None:
        """Set the output formatter, falling back to text for bad config."""
        if log_format == "gcp_json":
            formatter = GCPStructuredLogFormatter()
        else:
            from agent.redact import RedactingFormatter

            formatter = RedactingFormatter(_TEXT_LOG_FORMAT)
        self.setFormatter(formatter)


# ---------------------------------------------------------------------------
# Component metadata retained for filtering and observability consumers
# ---------------------------------------------------------------------------

class _ComponentFilter(logging.Filter):
    """Only pass records whose logger name starts with one of *prefixes*."""

    def __init__(self, prefixes: Sequence[str]) -> None:
        super().__init__()
        self._prefixes = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self._prefixes)


COMPONENT_PREFIXES = {
    "gateway": ("gateway", "hermes_plugins", "plugins.platforms"),
    "agent": ("agent", "run_agent", "model_tools", "batch_runner"),
    "tools": ("tools",),
    "cli": ("hermes_cli", "cli"),
    "cron": ("cron",),
    "gui": (
        "hermes_cli.web_server",
        "hermes_cli.pty_bridge",
        "tui_gateway",
        "uvicorn",
    ),
}


# ---------------------------------------------------------------------------
# Main setup
# ---------------------------------------------------------------------------

def setup_logging(
    *,
    hermes_home: Optional[Path] = None,
    log_level: Optional[str] = None,
    max_size_mb: Optional[int] = None,
    backup_count: Optional[int] = None,
    mode: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Install the configured stderr handler.

    ``hermes_home``, ``max_size_mb``, ``backup_count``, and ``mode`` remain in
    the signature for callers and plugins that use the historical API. Local
    file logging is intentionally no longer configured.

    The return value remains the profile's log directory path for API
    compatibility, but this function does not create that directory.
    """
    del max_size_mb, backup_count, mode
    global _logging_initialized
    cfg_level, _, _ = _read_logging_config()
    log_format = _read_logging_format()
    level_name = (log_level or cfg_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()

    handler = next(
        (
            h for h in root.handlers
            if getattr(h, _STRUCTURED_HANDLER_ATTR, False)
        ),
        None,
    )
    if handler is None:
        handler = GCPStructuredLogHandler(log_format=log_format)
        root.addHandler(handler)
    elif hasattr(handler, "set_log_format"):
        handler.set_log_format(log_format)
    handler.setLevel(level)

    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _logging_initialized = True
    home = hermes_home or get_hermes_home()
    return home / "logs"


def set_stream_log_level(level: int) -> None:
    """Change the level of the shared structured stream handler."""
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _STRUCTURED_HANDLER_ATTR, False):
            handler.setLevel(level)
    if level < root.level:
        root.setLevel(level)


def setup_verbose_logging() -> None:
    """Expose DEBUG records through the shared structured stream handler."""
    if not any(
        getattr(h, _STRUCTURED_HANDLER_ATTR, False)
        for h in logging.getLogger().handlers
    ):
        setup_logging()
    set_stream_log_level(logging.DEBUG)


# ---------------------------------------------------------------------------
# Compatibility helpers for callers and old integrations
# ---------------------------------------------------------------------------

def _add_rotating_handler(*args, **kwargs) -> None:
    """Deprecated no-op: Hermes no longer writes rotating log files."""
    del args, kwargs


def rotating_file_handlers() -> list:
    """Return an empty list because file handlers are no longer installed."""
    return []


def flush_log_queue() -> None:
    """Flush structured stream handlers synchronously."""
    for handler in logging.getLogger().handlers:
        if getattr(handler, _STRUCTURED_HANDLER_ATTR, False):
            try:
                handler.flush()
            except Exception:
                pass


def drain_log_queue(timeout: float = 1.0) -> None:
    """Compatibility shim for the former async file logger."""
    del timeout
    flush_log_queue()


def _reset_queued_handlers() -> None:
    """Test helper that removes Hermes-owned structured handlers."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if (
            getattr(handler, _STRUCTURED_HANDLER_ATTR, False)
            or getattr(handler, "_hermes_queue", False)
        ):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def _read_logging_config():
    """Best-effort read of the remaining ``logging.level`` setting."""
    cfg = _load_logging_config()
    log_cfg = cfg.get("logging", {})
    if isinstance(log_cfg, dict):
        return (log_cfg.get("level"), None, None)
    return (None, None, None)


def _read_logging_format() -> str:
    """Return the configured stream format, defaulting safely to text."""
    cfg = _load_logging_config()
    log_cfg = cfg.get("logging", {})
    if isinstance(log_cfg, dict) and log_cfg.get("format") == "gcp_json":
        return "gcp_json"
    return "text"


def _load_logging_config() -> dict:
    """Best-effort load of raw config for logging settings."""
    try:
        try:
            from hermes_cli.config import read_raw_config as _rrc

            cfg = _rrc() or {}
        except Exception:
            from utils import fast_safe_load

            config_path = get_config_path()
            if not config_path.exists():
                return {}
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = fast_safe_load(f) or {}

        try:
            from hermes_cli import managed_scope

            cfg = managed_scope.apply_managed_overlay(cfg)
        except Exception:
            pass

        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        pass
    return {}
