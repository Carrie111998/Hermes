"""E2B cloud execution environment (Vugola patch — not upstream yet).

Uses the E2B Python SDK to run commands in an E2B sandbox. Modeled on the
Daytona backend: spawn-per-call via _ThreadedProcessHandle wrapping blocking
SDK calls.

Two lifecycle modes:

- **create** (default): this environment creates a fresh sandbox from
  ``TERMINAL_E2B_TEMPLATE`` and kills it on cleanup().
- **attach**: when ``TERMINAL_E2B_SANDBOX_ID`` is set, the environment drives
  an existing sandbox owned by an outer orchestrator (e.g. the Vugola worker,
  which creates the sandbox before the job and kills it after settlement).
  cleanup() then does NOT kill the sandbox — the owner does.

Auth: ``E2B_API_KEY`` from the process environment. The sandbox itself never
receives credentials (Vugola zero-credential rule); this module runs on the
credentialed host, not in the sandbox.

Deliberately NOT wired: FileSyncManager (no ~/.hermes push into the sandbox).
The sandbox is a per-job work computer, not the brain host — the brain
(memories/skills/state.db) stays on the harness host under $HERMES_HOME.
"""

import logging
import os
import threading

from tools.environments.base import (
    BaseEnvironment,
    _ThreadedProcessHandle,
)

logger = logging.getLogger(__name__)


class E2BEnvironment(BaseEnvironment):
    """E2B cloud sandbox execution backend."""

    _stdin_mode = "heredoc"

    # Sliding sandbox TTL. E2B's create/set_timeout value is an ABSOLUTE
    # expiry (unlike hermes' lifetime_seconds inactivity reaper), so we renew
    # it before every command; a long agent turn (model thinking, rate-limit
    # backoff) between commands therefore has this long before the box reaps.
    _TTL_SECONDS_DEFAULT = 1800

    def __init__(
        self,
        template: str = "",
        cwd: str = "/home/user",
        timeout: int = 60,
        task_id: str = "default",
        sandbox_id: str = "",
        ttl_seconds: int = 0,
    ):
        super().__init__(cwd=cwd, timeout=timeout)

        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("terminal.e2b", prompt=False)
        except ImportError:
            pass
        except Exception as e:
            raise ImportError(str(e))
        from e2b import Sandbox

        if not os.environ.get("E2B_API_KEY"):
            raise ValueError("E2B environment requires E2B_API_KEY in the process environment")

        self._ttl_seconds = int(ttl_seconds) if int(ttl_seconds or 0) > 0 else self._TTL_SECONDS_DEFAULT
        self._lock = threading.Lock()
        self._owns_sandbox = not sandbox_id
        if sandbox_id:
            # Sandbox.connect() is the sanctioned attach API; the bare Sandbox()
            # constructor is deprecated internal surface.
            self._sandbox = Sandbox.connect(sandbox_id)
            logger.info("E2B: attached to sandbox %s for task %s (owner: external)",
                        sandbox_id, task_id)
        else:
            if not template:
                # Fail closed at startup: the default E2B base image lacks the
                # job toolkit (ffmpeg/opencv); falling back silently would fail
                # deep inside the run instead of here.
                raise ValueError(
                    "E2B environment requires an explicit template "
                    "(TERMINAL_E2B_TEMPLATE / terminal.e2b_template)"
                )
            self._sandbox = Sandbox.create(
                template=template,
                timeout=self._ttl_seconds,
            )
            logger.info("E2B: created sandbox %s from template %r for task %s (ttl %ss, sliding)",
                        self._sandbox.sandbox_id, template, task_id, self._ttl_seconds)

        self.init_session()

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None):
        """Return a _ThreadedProcessHandle wrapping a blocking E2B SDK call."""
        import shlex

        sandbox = self._sandbox

        shell = "bash -l -c" if login else "bash -c"
        shell_cmd = f"{shell} {shlex.quote(cmd_string)}"

        ttl = self._ttl_seconds

        def exec_fn() -> tuple[str, int]:
            try:
                # Renew the absolute TTL so the box always has a full window
                # ahead of the command that is about to run (sliding lease).
                try:
                    sandbox.set_timeout(ttl)
                except Exception:
                    pass  # renewal is best-effort; the command itself decides
                res = sandbox.commands.run(shell_cmd, timeout=max(1, timeout))
                out = (res.stdout or "") + (res.stderr or "")
                return (out, res.exit_code or 0)
            except Exception as e:  # CommandExitException carries the streams
                stdout = getattr(e, "stdout", "") or ""
                stderr = getattr(e, "stderr", "") or ""
                exit_code = getattr(e, "exit_code", None)
                if exit_code is None:
                    raise
                return (stdout + stderr, int(exit_code))

        def cancel():
            # Per-command interrupt/timeout. Deliberately NOT sandbox.kill():
            # the base class invokes cancel_fn on every command timeout, and
            # destroying the box would turn one slow ffmpeg pass into a dead
            # session (every later command failing on a vanished sandbox).
            # The SDK's own commands.run(timeout=...) terminates the remote
            # command; the sandbox survives for the rest of the conversation
            # and its sliding TTL reaps it if the session dies outright.
            logger.info("E2B: command cancelled/timed out (sandbox kept alive)")

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    def cleanup(self):
        with self._lock:
            if self._sandbox is None:
                return
            if self._owns_sandbox:
                try:
                    self._sandbox.kill()
                    logger.info("E2B: killed sandbox %s", self._sandbox.sandbox_id)
                except Exception as e:
                    logger.warning("E2B: cleanup failed (sandbox TTL will reap it): %s", e)
            else:
                logger.info("E2B: detaching from externally owned sandbox %s",
                            self._sandbox.sandbox_id)
            self._sandbox = None
