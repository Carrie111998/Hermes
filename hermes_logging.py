"""Centralized logging setup for Hermes Agent.

Provides a single ``setup_logging()`` entry point that both the CLI and
gateway call early in their startup path.  All log files live under
``~/.hermes/logs/`` (profile-aware via ``get_hermes_home()``).

Log files produced:
    agent.log              — INFO+, all agent/tool/session activity (main log)
    errors.log             — WARNING+, errors and warnings only (quick triage)
    gateway.log            — INFO+, gateway-only events (mode="gateway")
    gateway-forensics.log  — INFO+, unfiltered catch-all when mode="gateway",
                             independent of stdout/stderr capture by the
                             wrapper that spawned the gateway. Path
                             overridable via ``HERMES_GATEWAY_LOG_FILE``
                             (read from ``~/.hermes/.env`` — root, NOT
                             profile-scoped — see hermes_cli/env_loader.py).
    gui.log                — INFO+, dashboard/websocket/TUI-gateway events
                             (created when mode="gui").

All files use ``RotatingFileHandler`` with ``RedactingFormatter`` so
secrets are never written to disk.

Component separation:
    gateway.log only receives records from ``gateway.*`` loggers —
    platform adapters, session management, slash commands, delivery.
    gui.log receives dashboard-side records from ``hermes_cli.web_server``,
    ``hermes_cli.pty_bridge``, ``tui_gateway.*``, and ``uvicorn.*``.
    agent.log remains the catch-all (everything goes there).
    gateway-forensics.log is also unfiltered but only attached when
    mode="gateway" — gives forensics consumers a stable, gateway-process-
    scoped grep target without competing with agent.log's CLI activity.

Session context:
    Call ``set_session_context(session_id)`` at the start of a conversation
    and ``clear_session_context()`` when done.  All log lines emitted on
    that thread will include ``[session_id]`` for filtering/correlation.
"""

import io
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Sequence

# On Windows, stdlib ``RotatingFileHandler`` calls ``os.rename()`` in
# ``doRollover()`` and fails with ``PermissionError [WinError 32]`` whenever
# another process holds an append-mode handle on ``agent.log`` — which is
# essentially always in Hermes (TUI, gateway, ``hy_memory`` server, MCP
# servers, and on-demand CLI commands all log from separate processes),
# pinning ``agent.log`` at the 5 MiB threshold and spamming stderr with
# a traceback on every emit. ``concurrent-log-handler`` wraps the rename in a
# cross-process file lock (via ``portalocker``: pywin32 on Windows) so only
# one process rotates at a time and the others wait their turn.
#
# This swap is Windows-ONLY and deliberately so:
#   * The bug (WinError 32 on rename-while-open) is specific to Windows file
#     locking semantics — POSIX renames an open file fine, so stdlib already
#     works correctly on Linux/macOS.
#   * On POSIX, managed-mode (NixOS) relies on the exact ``_open()`` /
#     ``doRollover()`` lifecycle of stdlib ``RotatingFileHandler`` (the
#     ``_ManagedRotatingFileHandler`` subclass chmods 0660 after each). CLH
#     opens lazily and rotates differently, which breaks the group-writable
#     guarantee and the eager file-creation those paths depend on.
# Aliasing keeps every existing ``RotatingFileHandler`` reference in this
# module (class declaration, ``isinstance`` checks, docstring) working
# unchanged. See #44873.
if sys.platform == "win32":
    from concurrent_log_handler import (  # noqa: E402
        ConcurrentRotatingFileHandler as RotatingFileHandler,
    )
else:
    from logging.handlers import RotatingFileHandler  # noqa: E402


from hermes_constants import get_config_path, get_hermes_home

# Sentinel to track whether setup_logging() has already run.  The function
# is idempotent — calling it twice is safe but the second call is a no-op
# unless ``force=True``.
_logging_initialized = False

# Thread-local storage for per-conversation session context.
_session_context = threading.local()

# Default log format — includes timestamp, level, optional session tag,
# logger name, and message.  The ``%(session_tag)s`` field is guaranteed to
# exist on every LogRecord via _install_session_record_factory() below.
_LOG_FORMAT = "%(asctime)s %(levelname)s%(session_tag)s %(name)s: %(message)s"
_LOG_FORMAT_VERBOSE = "%(asctime)s - %(name)s - %(levelname)s%(session_tag)s - %(message)s"


def _safe_stderr():  # type: ignore[return]
    """Return a stderr stream that tolerates Unicode on all platforms.

    On Windows the console encoding is often a legacy MBCS codec
    (cp949, cp1252, …) that raises ``UnicodeEncodeError`` for characters
    like the em-dash (U+2014).  We wrap ``sys.stderr`` in a
    ``TextIOWrapper`` with ``errors='replace'`` so log lines are never
    lost — un-encodable characters are replaced with ``?`` instead of
    crashing the process.
    """
    stream = sys.stderr
    encoding = getattr(stream, "encoding", None) or "utf-8"
    # Already UTF-8 or surrogate-aware — no wrapping needed.
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
            # Prevent the wrapper from closing the underlying buffer
            # when it is garbage-collected.
            wrapped.close = lambda: None  # type: ignore[assignment]
            return wrapped
    except Exception:
        pass
    # Best-effort: if wrapping fails, return the original stream.
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
    """Set the session ID for the current thread.

    All subsequent log records on this thread will include ``[session_id]``
    in the formatted output.  Call at the start of ``run_conversation()``.
    """
    _session_context.session_id = session_id


def clear_session_context() -> None:
    """Clear the session ID for the current thread."""
    _session_context.session_id = None


# ---------------------------------------------------------------------------
# Record factory — injects session_tag into every LogRecord at creation
# ---------------------------------------------------------------------------

def _install_session_record_factory() -> None:
    """Replace the global LogRecord factory with one that adds ``session_tag``.

    Unlike a ``logging.Filter`` on a handler or logger, the record factory
    runs for EVERY record in the process — including records that propagate
    from child loggers and records handled by third-party handlers.  This
    guarantees ``%(session_tag)s`` is always available in format strings,
    eliminating the KeyError that would occur if a handler used our format
    without having a ``_SessionFilter`` attached.

    Idempotent — checks for a marker attribute to avoid double-wrapping if
    the module is reloaded.
    """
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_hermes_session_injector", False):
        return  # already installed

    def _session_record_factory(*args, **kwargs):
        record = current_factory(*args, **kwargs)
        sid = getattr(_session_context, "session_id", None)
        record.session_tag = f" [{sid}]" if sid else ""  # type: ignore[attr-defined]
        return record

    _session_record_factory._hermes_session_injector = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(_session_record_factory)


# Install immediately on import — session_tag is available on all records
# from this point forward, even before setup_logging() is called.
_install_session_record_factory()


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class _ComponentFilter(logging.Filter):
    """Only pass records whose logger name starts with one of *prefixes*.

    Used to route gateway-specific records to ``gateway.log`` while
    keeping ``agent.log`` as the catch-all.
    """

    def __init__(self, prefixes: Sequence[str]) -> None:
        super().__init__()
        self._prefixes = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self._prefixes)


# Logger name prefixes that belong to each component.
# Used by _ComponentFilter and exposed for ``hermes logs --component``.
COMPONENT_PREFIXES = {
    "gateway": ("gateway", "hermes_plugins"),
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
# Daemon role inference
# ---------------------------------------------------------------------------

# Long-lived singleton daemons that each hold the catch-all log open for their
# whole lifetime. On Windows that shared handle blocks log rotation, so each
# gets its own per-role catch-all file (see setup_logging ``role``). Keyed by
# the process *subcommand* (first positional argv token) so that tailing
# commands like ``hermes logs gateway`` are NOT misclassified as the daemon.
_DAEMON_SUBCOMMAND_ROLES = {
    "gateway": "gateway",
    "dashboard": "dashboard",
    "proxy": "proxy",
}


def infer_daemon_role(argv: Optional[Sequence[str]] = None) -> Optional[str]:
    """Best-effort daemon role from a process's argv, else ``None``.

    Pure (argv-only) so it is deterministic and unit-testable; the CLI import
    sites pass the result to ``setup_logging(role=...)``. Returns ``None``
    for transient ``cli``/``cron`` processes, which keep the shared
    ``agent.log``.
    """
    argv = list(sys.argv if argv is None else argv)
    if argv:
        prog = os.path.basename(argv[0])
        if prog.startswith("devflow_bridge_runner"):
            return "devflow-bridge"
    # Walk argv[1:] skipping flag tokens (starting with "-") and the value
    # token that immediately follows a long flag (e.g. ``--profile main``).
    # This ensures ``hermes --profile main gateway run`` resolves to "gateway"
    # rather than "main".
    # Best-effort: short flags (single dash) are treated as valueless;
    # only ``--long value`` pairs consume the following token. Real daemon
    # launch commands put the subcommand first, so this suffices.
    skip_next = False
    for tok in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            # Long flag without inline value (``--flag``, not ``--flag=val``)?
            # If so, the *next* positional is its value — skip it too.
            if tok.startswith("--") and "=" not in tok:
                skip_next = True
            continue
        return _DAEMON_SUBCOMMAND_ROLES.get(tok)
    return None


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
    role: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Configure the Hermes logging subsystem.

    Safe to call multiple times — the second call is a no-op unless
    *force* is ``True``.

    Parameters
    ----------
    hermes_home
        Override for the Hermes home directory.  Falls back to
        ``get_hermes_home()`` (profile-aware).
    log_level
        Minimum level for the ``agent.log`` file handler.  Accepts any
        standard Python level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``).
        Defaults to ``"INFO"`` or the value from config.yaml ``logging.level``.
    max_size_mb
        Maximum size of each log file in megabytes before rotation.
        Defaults to 5 or the value from config.yaml ``logging.max_size_mb``.
    backup_count
        Number of rotated backup files to keep.
        Defaults to 3 or the value from config.yaml ``logging.backup_count``.
    mode
        Caller context: ``"cli"``, ``"gateway"``, ``"gui"``, ``"cron"``.
        When ``"gateway"``, an additional ``gateway.log`` file is created
        that receives only gateway-component records.
        When ``"gui"``, an additional ``gui.log`` file is created that
        receives dashboard and TUI-gateway component records.
    role
        Per-role catch-all routing. When set, ``agent.log``/``errors.log``
        become ``agent-<role>.log``/``errors-<role>.log`` so a long-lived
        daemon owns its own files and Windows rotation is never blocked by a
        sibling's open handle. ``None`` (transient cli/cron) keeps the shared
        files. ``mode="gateway"`` defaults this to ``"gateway"``.
    force
        Re-run setup even if it has already been called.

    Returns
    -------
    Path
        The ``logs/`` directory where files are written.
    """
    global _logging_initialized
    home = hermes_home or get_hermes_home()
    log_dir = home / "logs"

    # Daemon processes route their catch-all logs to per-role files so each
    # is the sole long-lived holder and rotation is not blocked by a sibling
    # process's open handle (Windows). mode="gateway" implies the gateway
    # role unless an explicit role was passed.
    if role is None and mode == "gateway":
        role = "gateway"
    _role_suffix = f"-{role}" if role else ""
    agent_log_path = log_dir / f"agent{_role_suffix}.log"
    errors_log_path = log_dir / f"errors{_role_suffix}.log"

    # Lazy import to avoid circular dependency at module load time.
    from agent.redact import RedactingFormatter

    root = logging.getLogger()

    # Global, mode-independent handlers (agent.log + errors.log) and
    # noise/level config run only on first call. Subsequent calls skip
    # this block — _add_rotating_handler is per-path idempotent anyway,
    # but skipping avoids re-reading config and re-applying noise filters.
    if not _logging_initialized or force:
        log_dir.mkdir(parents=True, exist_ok=True)

        # Read config defaults (best-effort — config may not be loaded yet).
        cfg_level, cfg_max_size, cfg_backup = _read_logging_config()

        level_name = (log_level or cfg_level or "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        max_bytes = (max_size_mb or cfg_max_size or 5) * 1024 * 1024
        backups = backup_count or cfg_backup or 3

        # --- agent.log (INFO+) — the main activity log ---------------------
        _add_rotating_handler(
            root,
            agent_log_path,
            level=level,
            max_bytes=max_bytes,
            backup_count=backups,
            formatter=RedactingFormatter(_LOG_FORMAT),
        )

        # --- errors.log (WARNING+) — quick triage log ----------------------
        _add_rotating_handler(
            root,
            errors_log_path,
            level=logging.WARNING,
            max_bytes=2 * 1024 * 1024,
            backup_count=2,
            formatter=RedactingFormatter(_LOG_FORMAT),
        )

        # Ensure root logger level is low enough for the handlers to fire.
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)

        # Suppress noisy third-party loggers.
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

        _logging_initialized = True

    # Mode-specific handlers run on EVERY call so a later mode="gateway"
    # call can upgrade an earlier mode="cli" init. Production path:
    # hermes_cli/main.py:174 calls setup_logging(mode="cli") at module
    # import; gateway/run.py later calls setup_logging(mode="gateway").
    # Pre-fix the second call returned early because _logging_initialized
    # was True, leaving gateway.log dead since 2026-04-10. Per-path
    # idempotency in _add_rotating_handler prevents duplicates.
    if mode == "gateway":
        log_dir.mkdir(parents=True, exist_ok=True)

        # --- gateway.log (INFO+, gateway component only) -------------------
        _add_rotating_handler(
            root,
            log_dir / "gateway.log",
            level=logging.INFO,
            max_bytes=5 * 1024 * 1024,
            backup_count=3,
            formatter=RedactingFormatter(_LOG_FORMAT),
            log_filter=_ComponentFilter(COMPONENT_PREFIXES["gateway"]),
        )

        # --- gateway-forensics.log (INFO+, unfiltered) ---------------------
        # Belt-and-suspenders log independent of stdout/stderr capture by
        # the wrapper that spawned the gateway (Windows Start-Detached
        # redirect has been observed to silently drop output — see memory
        # ``windows_start_process_detach_trap.md``). Captures every record
        # at INFO+ from any logger so subscriber-loop and WAL-contention
        # diagnostics show up even when they originate from non-gateway
        # loggers (events.*, etc.). Path overridable via
        # ``HERMES_GATEWAY_LOG_FILE`` (read from ~/.hermes/.env, NOT
        # profile-scoped — see ``hermes_cli/env_loader.py``).
        # Wrapped in try/except because the path comes from user env input
        # and a bad value must not crash the gateway — the curated handlers
        # above remain attached either way.
        try:
            forensics_path = _gateway_forensics_path(log_dir)
            _add_rotating_handler(
                root,
                forensics_path,
                level=logging.INFO,
                max_bytes=10 * 1024 * 1024,
                backup_count=5,
                formatter=RedactingFormatter(_LOG_FORMAT),
            )
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning(
                "gateway-forensics handler failed to attach (HERMES_GATEWAY_LOG_FILE=%r): %s",
                os.environ.get("HERMES_GATEWAY_LOG_FILE"),
                exc,
            )

    # --- gui.log (INFO+, dashboard/tui-gateway components) -----------------
    if mode == "gui":
        _add_rotating_handler(
            root,
            log_dir / "gui.log",
            level=logging.INFO,
            max_bytes=10 * 1024 * 1024,
            backup_count=5,
            formatter=RedactingFormatter(_LOG_FORMAT),
            log_filter=_ComponentFilter(COMPONENT_PREFIXES["gui"]),
        )

    return log_dir


def _gateway_forensics_path(default_dir: Path) -> Path:
    """Resolve the gateway forensics log path.

    Priority:
        1. ``HERMES_GATEWAY_LOG_FILE`` env var (absolute or ``~``-rooted)
        2. ``<default_dir>/gateway-forensics.log``
    """
    override = os.environ.get("HERMES_GATEWAY_LOG_FILE")
    if override:
        return Path(override).expanduser()
    return default_dir / "gateway-forensics.log"


def setup_verbose_logging() -> None:
    """Enable DEBUG-level console logging for ``--verbose`` / ``-v`` mode.

    Called by ``AIAgent.__init__()`` when ``verbose_logging=True``.
    """
    from agent.redact import RedactingFormatter

    root = logging.getLogger()

    # Avoid adding duplicate stream handlers.
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
            if getattr(h, "_hermes_verbose", False):
                return

    handler = logging.StreamHandler(_safe_stderr())
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT_VERBOSE, datefmt="%H:%M:%S"))
    handler._hermes_verbose = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    # Lower root logger level so DEBUG records reach all handlers.
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)

    # Keep third-party libraries at WARNING to reduce noise.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # rex-deploy at INFO for sandbox status.
    logging.getLogger("rex-deploy").setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# --- Rollover resilience tuning --------------------------------------------
# agent.log is opened by EVERY Hermes process: the gateway, plus each
# ``hermes`` CLI/cron subprocess (setup_logging(mode="cli") runs at
# hermes_cli/main.py import) and any log reader/tail. On Windows an open
# handle in *any* process makes ``os.rename`` fail with PermissionError
# [WinError 32]. We retry briefly to ride out transient sibling holds, and if
# the file stays locked we defer rotation for a cooldown instead of
# re-attempting (and dumping a traceback to stderr) on every single emit.
_ROLLOVER_RETRY_ATTEMPTS = 4
_ROLLOVER_RETRY_DELAY_SEC = 0.05
_ROLLOVER_COOLDOWN_SEC = 30.0


class _ManagedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler hardened for managed perms, Windows log sharing,
    AND external rotation.

    Three behaviours layered on the stdlib handler (merge of the fork's
    Windows-safe rotation with upstream 0.16.0's external-rotation reopen):

    1. **Managed-mode perms.** In managed mode (NixOS), the stateDir uses
       setgid (2770) so new files inherit the hermes group. However both
       _open() (initial creation) and doRollover() create files via open(),
       which uses the process umask — typically 0022, producing 0644. This
       subclass applies chmod 0660 after both operations so the gateway and
       interactive users can share log files.

    2. **Windows-safe rotation (fork).** The stdlib ``doRollover`` shuffles and
       removes the backup chain (``.2``→``.3``, ``.1``→``.2``, ``rm .1``)
       *before* the ``base``→``.1`` rename. On Windows that rename fails with
       PermissionError [WinError 32] whenever another process/reader holds
       the file open — and the stdlib then (a) raises, so ``logging`` dumps a
       full traceback to stderr on EVERY emit once the file is pinned at
       maxBytes, and (b) leaves the stream closed, while the backups it has
       already shuffled are lost. Observed live: agent.log pinned at 5 MB
       with agent.log.1/.2 erased.

       This override instead moves the *base* file aside to a temp name
       FIRST — the only step that can hit the cross-process lock — and only
       touches the backup chain once that succeeds. On a persistent lock it
       reopens the stream (logging never stops), leaves the backups
       untouched, defers further attempts for a cooldown, and logs ONE
       concise warning rather than a traceback storm. Rotation simply resumes
       the next time the lock clears.

    3. **External-rotation reopen (upstream 0.16.0).** ``RotatingFileHandler``
       keeps an open file descriptor. If anything rotates the file *externally*
       (``logrotate``, manual ``mv``, another process rotating under us, a
       transient unlink), our fd keeps pointing at the renamed/unlinked inode
       and every subsequent write goes to ``gateway.log.1`` instead of
       ``gateway.log`` — silent log loss. Before each emit we ``stat``
       ``baseFilename`` and compare it against the open stream's inode; on
       mismatch we reopen (same pattern as stdlib
       ``WatchedFileHandler.reopenIfNeeded()``, adapted for rotating handlers).
    """

    def __init__(self, *args, **kwargs):
        from hermes_cli.config import is_managed
        self._managed = is_managed()
        # Monotonic deadline before which shouldRollover() stays quiet after a
        # locked rollover (0.0 == not deferred), plus a one-warning-per-window
        # latch and the last lock error for the warning message.
        self._rollover_blocked_until = 0.0
        self._rollover_warned = False
        self._last_rollover_error: Optional[BaseException] = None
        super().__init__(*args, **kwargs)
        # Snapshot the inode of the currently open stream so emit() can
        # detect external rotation without an extra fstat per write.
        self._stat_dev: Optional[int] = None
        self._stat_ino: Optional[int] = None
        self._record_stream_stat()

    def _chmod_if_managed(self):
        if self._managed:
            try:
                os.chmod(self.baseFilename, 0o660)
            except OSError:
                pass

    def _record_stream_stat(self) -> None:
        """Snapshot dev/ino of ``baseFilename`` so we can detect external rotation."""
        try:
            st = os.stat(self.baseFilename)
            self._stat_dev, self._stat_ino = st.st_dev, st.st_ino
        except OSError:
            self._stat_dev, self._stat_ino = None, None

    def _reopen_if_externally_rotated(self) -> None:
        """Reopen the stream when ``baseFilename`` no longer matches our fd.

        Triggered when ``baseFilename`` was renamed (logrotate), unlinked,
        or replaced by a different inode.  Silent + best-effort: any error
        falls back to the existing (possibly stale) stream so logging keeps
        working instead of dying on a stat failure.
        """
        try:
            st = os.stat(self.baseFilename)
        except FileNotFoundError:
            # File was rotated/unlinked underneath us.  Close + reopen so a
            # fresh inode is created at the expected path.
            try:
                if self.stream is not None:
                    self.stream.close()
            except Exception:
                pass
            self.stream = None  # type: ignore[assignment]
            try:
                self.stream = self._open()
                self._record_stream_stat()
            except Exception:
                # Couldn't reopen — leave stream=None; next emit will
                # bail rather than write to a stale inode.
                pass
            return
        except OSError:
            return  # transient — try again on the next emit

        if self._stat_dev is None or self._stat_ino is None:
            self._stat_dev, self._stat_ino = st.st_dev, st.st_ino
            return

        if (st.st_dev, st.st_ino) != (self._stat_dev, self._stat_ino):
            # baseFilename now points at a DIFFERENT inode than the one we
            # hold open.  Close the old stream and open the new file.
            try:
                if self.stream is not None:
                    self.stream.close()
            except Exception:
                pass
            self.stream = None  # type: ignore[assignment]
            try:
                self.stream = self._open()
                self._stat_dev, self._stat_ino = st.st_dev, st.st_ino
            except Exception:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        # Cheap-ish stat-per-record check; the kernel caches inode metadata
        # so the syscall is sub-microsecond on a hot file.
        if self.stream is not None or os.path.exists(self.baseFilename):
            self._reopen_if_externally_rotated()
        super().emit(record)

    def _open(self):
        stream = super()._open()
        self._chmod_if_managed()
        return stream

    # -- Windows-safe rotation ------------------------------------------

    def shouldRollover(self, record):
        # While a previous rollover is locked out, keep appending rather than
        # re-attempting (and re-failing) on every record.
        if self._rollover_blocked_until and time.monotonic() < self._rollover_blocked_until:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        # Close our own handle first so this process isn't the blocker.
        if self.stream:
            try:
                self.stream.close()
            finally:
                self.stream = None

        rotated = self._rotate_lock_safe()

        # ALWAYS reopen so logging keeps working, rotated or not. (The stdlib
        # only reopens after a successful rename; on failure it leaves the
        # stream closed, which is what turned the lock into a traceback storm.)
        if not self.delay:
            try:
                self.stream = self._open()
            except OSError:
                self.stream = None
        self._chmod_if_managed()
        # Our own rollover writes a new baseFilename; refresh the snapshot
        # so the next emit doesn't mistake it for external rotation.
        self._record_stream_stat()

        if rotated:
            self._rollover_blocked_until = 0.0
            self._rollover_warned = False
        else:
            self._rollover_blocked_until = time.monotonic() + _ROLLOVER_COOLDOWN_SEC
            self._warn_rollover_deferred()

    def _rotate_lock_safe(self) -> bool:
        """Rotate the base file without ever corrupting the backup chain.

        Returns True if rotation completed, False if the base file is held
        open by another process/reader (rotation deferred; on-disk backups
        left exactly as they were).
        """
        if self.backupCount <= 0:
            # No backups requested → the stdlib just reopens in append mode,
            # which does not shrink the file. Nothing to rotate.
            return True
        if not os.path.exists(self.baseFilename):
            return True  # nothing written yet

        # Step 1: move the base aside to a temp name. This is the ONLY step
        # that hits the cross-process Windows lock, and it touches no backups,
        # so a failure here leaves agent.log.1..N untouched.
        tmp = self.baseFilename + ".rotating"
        if not self._replace_with_retry(self.baseFilename, tmp):
            return False  # locked → defer; backups intact

        # Step 2: the base is now free. Shuffle the backup chain and drop the
        # rotated content into .1. These files were just created/owned by us,
        # so they don't hit the cross-process lock; os.replace overwrites
        # atomically (no remove-then-rename gap).
        for i in range(self.backupCount - 1, 0, -1):
            sfn = self.rotation_filename("%s.%d" % (self.baseFilename, i))
            dfn = self.rotation_filename("%s.%d" % (self.baseFilename, i + 1))
            if os.path.exists(sfn):
                try:
                    os.replace(sfn, dfn)
                except OSError:
                    pass  # a held backup is non-fatal; .1 still lands below
        dfn = self.rotation_filename(self.baseFilename + ".1")
        try:
            os.replace(tmp, dfn)
        except OSError:
            # Extremely unlikely (tmp is ours). Restore the live content so we
            # don't lose it, and report the rollover as deferred.
            try:
                os.replace(tmp, self.baseFilename)
            except OSError:
                pass
            return False
        return True

    def _replace_with_retry(self, src: str, dst: str) -> bool:
        """``os.replace(src, dst)`` with bounded retry for transient locks."""
        self._last_rollover_error = None
        for attempt in range(_ROLLOVER_RETRY_ATTEMPTS):
            try:
                os.replace(src, dst)
                return True
            except (PermissionError, OSError) as exc:
                self._last_rollover_error = exc
                if attempt < _ROLLOVER_RETRY_ATTEMPTS - 1:
                    time.sleep(_ROLLOVER_RETRY_DELAY_SEC)
        return False

    def _warn_rollover_deferred(self):
        # _rollover_blocked_until is already set, so this warning's own emit
        # cannot re-enter doRollover. Warn once per cooldown window.
        if self._rollover_warned:
            return
        self._rollover_warned = True
        logging.getLogger(__name__).warning(
            "Log rotation for %s deferred ~%.0fs: file held open by another "
            "process or reader (%s). It may exceed %d bytes until the lock "
            "clears; rotated backups are untouched.",
            self.baseFilename,
            _ROLLOVER_COOLDOWN_SEC,
            self._last_rollover_error,
            self.maxBytes,
        )


def _add_rotating_handler(
    logger: logging.Logger,
    path: Path,
    *,
    level: int,
    max_bytes: int,
    backup_count: int,
    formatter: logging.Formatter,
    log_filter: Optional[logging.Filter] = None,
) -> None:
    """Add a ``RotatingFileHandler`` to *logger*, skipping if one already
    exists for the same resolved file path (idempotent).

    Parameters
    ----------
    log_filter
        Optional filter to attach to the handler (e.g. ``_ComponentFilter``
        for gateway.log).
    """
    resolved = path.resolve()
    for existing in logger.handlers:
        if (
            isinstance(existing, RotatingFileHandler)
            and Path(getattr(existing, "baseFilename", "")).resolve() == resolved
        ):
            return  # already attached

    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _ManagedRotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    if log_filter is not None:
        handler.addFilter(log_filter)
    logger.addHandler(handler)


def _read_logging_config():
    """Best-effort read of ``logging.*`` from config.yaml.

    Returns ``(level, max_size_mb, backup_count)`` — any may be ``None``.
    """
    try:
        import yaml
        config_path = get_config_path()
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            # Managed scope: an administrator can pin logging.* too. Overlay via
            # the shared helper (fail-open) since this reads config.yaml directly.
            try:
                from hermes_cli import managed_scope
                cfg = managed_scope.apply_managed_overlay(cfg)
            except Exception:
                pass
            log_cfg = cfg.get("logging", {})
            if isinstance(log_cfg, dict):
                return (
                    log_cfg.get("level"),
                    log_cfg.get("max_size_mb"),
                    log_cfg.get("backup_count"),
                )
    except Exception:
        pass
    return (None, None, None)
