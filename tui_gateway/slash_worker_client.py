"""Persistent slash-command worker client.

God-file slice R1-S1 (epic #78647, target #78630): ``_SlashWorker`` and its
timeout constant were moved verbatim out of ``tui_gateway/server.py`` (the
13.9k-line TUI gateway god-file) into this sibling module. The class is public
here as ``SlashWorker``; ``tui_gateway.server`` re-exports it under the legacy
``_SlashWorker`` alias so handler rebinds, install-time pins and tests keep
working unchanged (see R1-CONSENSUS.md).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading

from tools.environments.local import hermes_subprocess_env

logger = logging.getLogger("tui_gateway.server")

try:
    _slash_timeout = float(os.environ.get("HERMES_TUI_SLASH_TIMEOUT_S") or "45")
except (ValueError, TypeError):
    _slash_timeout = 45.0
_SLASH_WORKER_TIMEOUT_S = max(5.0, _slash_timeout)



class SlashWorker:
    """Persistent HermesCLI subprocess for slash commands."""

    def __init__(self, session_key: str, model: str, profile_home: str | None = None):
        self._lock = threading.Lock()
        self._seq = 0
        self.stderr_tail: list[str] = []
        self.stdout_queue: queue.Queue[dict | None] = queue.Queue()

        argv = [
            sys.executable,
            "-m",
            "tui_gateway.slash_worker",
            "--session-key",
            session_key,
        ]
        if model:
            argv += ["--model", model]

        self._closed = False
        from hermes_cli._subprocess_compat import windows_hide_flags

        # slash_worker runs the Hermes agent → needs provider credentials.
        # Tier-1 secrets (gateway/GitHub/infra) are still stripped (#29157).
        # Global-remote / multi-profile sessions: the worker must resolve
        # config/skills/state against the session's profile home, not the
        # gateway's launch HERMES_HOME (#40677). The override goes through the
        # build_subprocess_env factory's `extra` (applied last, always wins)
        # instead of a hand-rolled env["HERMES_HOME"] assignment.
        from tools.environments.local import build_subprocess_env
        env = build_subprocess_env(
            hermes_subprocess_env(inherit_credentials=True),
            scrub_secrets=False,
            inherit_profile_home=False,  # base already carries the HOME contract
            extra={"HERMES_HOME": str(profile_home)} if profile_home else None,
        )

        # start_new_session=True detaches the slash worker into its own
        # process group / session. Without this, the worker inherits the
        # gateway's pgid (= TUI parent PID). When mcp_tool's
        # _kill_orphaned_mcp_children races with slash_worker spawn and sweeps
        # the gateway's child set, it captures the worker PID, records the
        # inherited pgid, and killpg() then kills the TUI parent itself.
        # See agent/lsp/client.py for the symmetric LSP server fix and
        # tools/mcp_tool.py _filter_mcp_children for defense-in-depth.
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Force UTF-8 with lossy decoding so child output containing bytes
            # that are invalid in the system locale (e.g. GBK on Chinese
            # Windows) can't raise UnicodeDecodeError inside the drain threads
            # and crash the gateway. See #53137.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=os.getcwd(),
            env=env,
            creationflags=windows_hide_flags(),
            start_new_session=True,
        )
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self):
        for line in self.proc.stdout or []:
            try:
                self.stdout_queue.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.stdout_queue.put(None)

    def _drain_stderr(self):
        for line in self.proc.stderr or []:
            if text := line.rstrip("\n"):
                self.stderr_tail = (self.stderr_tail + [text])[-80:]

    def run(self, command: str) -> str:
        if self.proc.poll() is not None:
            raise RuntimeError("slash worker exited")

        with self._lock:
            self._seq += 1
            rid = self._seq
            self.proc.stdin.write(json.dumps({"id": rid, "command": command}) + "\n")
            self.proc.stdin.flush()

            while True:
                try:
                    msg = self.stdout_queue.get(timeout=_SLASH_WORKER_TIMEOUT_S)
                except queue.Empty:
                    raise RuntimeError("slash worker timed out")
                if msg is None:
                    break
                if msg.get("id") != rid:
                    continue
                if not msg.get("ok"):
                    raise RuntimeError(msg.get("error", "slash worker failed"))
                return str(msg.get("output", "")).rstrip()

            raise RuntimeError(
                f"slash worker closed pipe{': ' + chr(10).join(self.stderr_tail[-8:]) if self.stderr_tail else ''}"
            )

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        proc = self.proc
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except Exception:
                    proc.kill()
                    try:
                        proc.wait(timeout=1)  # reap the zombie SIGKILL leaves behind
                    except Exception:
                        pass
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
