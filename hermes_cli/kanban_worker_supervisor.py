"""Supervisor for dispatcher-spawned Kanban workers.

The dispatcher needs one long-lived PID to monitor. The actual Hermes worker
may exit cleanly because it hit its own max-turn budget before completing the
Kanban task; that should become a retryable continuation, not a crash-loop
failure. This wrapper owns periodic heartbeats and classifies that exit.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


MAX_ITERATION_MARKERS = (
    "Iteration budget exhausted",
    "Iteration budget reached",
    "Reached maximum iterations",
    "You've reached the maximum number of tool-calling iterations allowed",
)


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _remove_workspace_from_sys_path(workspace: str) -> None:
    """Avoid importing task workspace files as Python modules inside supervisor."""
    if not workspace:
        return
    workspace_abs = os.path.abspath(workspace)
    try:
        cwd_abs = os.path.abspath(os.getcwd())
    except OSError:
        cwd_abs = ""

    cleaned: list[str] = []
    for entry in sys.path:
        if entry == "":
            if cwd_abs and _same_path(cwd_abs, workspace_abs):
                continue
            cleaned.append(entry)
            continue
        if _same_path(entry, workspace_abs):
            continue
        cleaned.append(entry)
    sys.path[:] = cleaned


def _worker_env(workspace: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = "1"
    if not workspace:
        return env

    workspace_abs = os.path.abspath(workspace)
    pythonpath = env.get("PYTHONPATH")
    if pythonpath:
        safe_parts = [
            part
            for part in pythonpath.split(os.pathsep)
            if part and not _same_path(part, workspace_abs)
        ]
        if safe_parts:
            env["PYTHONPATH"] = os.pathsep.join(safe_parts)
        else:
            env.pop("PYTHONPATH", None)
    return env


def _tail_text(path: Path, max_bytes: int = 65536) -> str:
    try:
        with path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes), os.SEEK_SET)
            except OSError:
                pass
            return f.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _returncode_label(returncode: int | None) -> str:
    if returncode is None:
        return "returncode=unknown"
    if returncode < 0:
        sig_num = abs(int(returncode))
        try:
            sig_name = signal.Signals(sig_num).name
        except ValueError:
            sig_name = f"SIG{sig_num}"
        return f"signal={sig_name} returncode={returncode}"
    if returncode >= 128:
        sig_num = int(returncode) - 128
        try:
            sig_name = signal.Signals(sig_num).name
        except ValueError:
            sig_name = f"SIG{sig_num}"
        return f"returncode={returncode} ({sig_name})"
    return f"returncode={returncode}"


def _tail_failure_hint(tail: str) -> str:
    markers = (
        "KeyboardInterrupt",
        "Traceback (most recent call last)",
        "SIGTERM",
        "SIGINT",
        "TimeoutError",
        "RuntimeError",
        "ModuleNotFoundError",
        "ImportError",
        "Unknown skill",
        "Killed",
        "OOM",
    )
    for marker in markers:
        if marker in tail:
            return f"log_marker={marker}"
    for line in reversed(tail.splitlines()):
        clean = line.strip()
        if clean:
            return f"log_tail_last_line={clean[:300]}"
    return "log_tail_empty"


def _record_child_exit(
    *,
    task_id: str,
    returncode: int | None,
    log_path: Path,
    command: Sequence[str],
    started_at: float,
    reason: str = "worker_child_exit",
) -> bool:
    try:
        from hermes_cli import kanban_db as kb

        tail = _tail_text(log_path, max_bytes=32768)
        label = _returncode_label(returncode)
        hint = _tail_failure_hint(tail)
        error = f"{reason}: {label}; {hint}"
        metadata = {
            "reason": reason,
            "child_returncode": returncode,
            "returncode_label": label,
            "elapsed_seconds": max(0, int(time.time() - started_at)),
            "log_path": str(log_path),
            "log_tail": tail[-6000:] if tail else "",
            "command": list(command),
        }
        with contextlib.closing(kb.connect()) as conn:
            return bool(
                kb.record_worker_child_crash(
                    conn,
                    task_id,
                    error=error,
                    metadata=metadata,
                )
            )
    except Exception as exc:
        print(
            f"kanban worker supervisor: failed to record child exit for {task_id}: {exc}",
            file=sys.stderr,
        )
        return False


def _task_is_running(task_id: str) -> bool:
    try:
        from hermes_cli import kanban_db as kb

        with contextlib.closing(kb.connect()) as conn:
            task = kb.get_task(conn, task_id)
            return bool(task and task.status == "running")
    except Exception:
        return False


def _task_assignee(task_id: str) -> str:
    try:
        from hermes_cli import kanban_db as kb

        with contextlib.closing(kb.connect()) as conn:
            task = kb.get_task(conn, task_id)
            return str(task.assignee or "") if task else ""
    except Exception:
        return ""


def _block_task(task_id: str, reason: str) -> bool:
    try:
        from hermes_cli import kanban_db as kb

        with contextlib.closing(kb.connect()) as conn:
            return bool(kb.block_task(conn, task_id, reason=reason))
    except Exception as exc:
        print(f"kanban worker supervisor: failed to block {task_id}: {exc}", file=sys.stderr)
        return False


def _materials_token_guard_reason(task_id: str, log_path: Path, started_at: float) -> str | None:
    if _task_assignee(task_id) != "nf-materials-producer":
        return None

    elapsed = max(0, int(time.time() - started_at))
    max_seconds = int(os.getenv("HERMES_NF_MATERIALS_MAX_SECONDS", "1800"))
    if elapsed >= max_seconds:
        return (
            f"materials_token_guard: nf-materials-producer worker exceeded "
            f"{max_seconds}s runtime; stop routine material refill and hand off "
            f"owner+next_action instead of continuing."
        )

    tail = _tail_text(log_path, max_bytes=131072)
    compressed_counts = [int(x) for x in re.findall(r"Session compressed\s+(\d+)\s+times", tail)]
    max_compressed = max(compressed_counts) if compressed_counts else 0
    compact_markers = tail.count("compacting context")
    if max_compressed >= 2 or compact_markers >= 4:
        return (
            "materials_token_guard: nf-materials-producer worker exceeded "
            f"context budget (session_compressed={max_compressed}, "
            f"compact_markers={compact_markers}); stop routine material refill "
            "and hand off tooling/debug work instead of continuing."
        )
    return None


def _heartbeat(task_id: str, ttl_seconds: int, note: str) -> None:
    try:
        from hermes_cli import kanban_db as kb

        with contextlib.closing(kb.connect()) as conn:
            kb.heartbeat_worker(conn, task_id, note=note)
    except Exception:
        pass


def _release_if_profile_quiet(task_id: str) -> bool:
    try:
        from hermes_cli import kanban_db as kb

        with contextlib.closing(kb.connect()) as conn:
            task = kb.get_task(conn, task_id)
            if not task or not task.assignee:
                return False
            quiet_mode = kb._profile_quiet_mode(task.assignee)
            if not quiet_mode:
                return False
            marker_path, quiet_reason = quiet_mode
            summary = (
                f"Worker launch paused because assignee profile {task.assignee!r} "
                f"is in quiet mode: {quiet_reason}"
            )
            kb.release_task_for_retry(
                conn,
                task_id,
                reason="profile_quiet_mode_paused",
                summary=summary,
                metadata={
                    "assignee": task.assignee,
                    "marker_path": marker_path,
                    "quiet_reason": quiet_reason,
                },
            )
            print(f"kanban worker supervisor: {summary}", file=sys.stderr)
            return True
    except Exception as exc:
        print(f"kanban worker supervisor: quiet-mode check failed: {exc}", file=sys.stderr)
        return False


def _release_if_budget_exhausted(task_id: str, log_path: Path) -> bool:
    tail = _tail_text(log_path)
    if not any(marker in tail for marker in MAX_ITERATION_MARKERS):
        return False
    try:
        from hermes_cli import kanban_db as kb

        summary = (
            "Worker hit its max-iteration budget before completing the task; "
            "released for an automatic continuation."
        )
        with contextlib.closing(kb.connect()) as conn:
            return kb.release_task_for_retry(
                conn,
                task_id,
                reason="max_iterations_exhausted",
                summary=summary,
                metadata={"log_path": str(log_path)},
            )
    except Exception:
        return False


def _terminate_child(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_supervisor(
    *,
    task_id: str,
    ttl_seconds: int,
    heartbeat_interval_seconds: int,
    log_path: Path,
    workspace: str,
    command: Sequence[str],
) -> int:
    if not command:
        print("kanban worker supervisor: missing child command", file=sys.stderr)
        return 2
    heartbeat_interval_seconds = max(30, int(heartbeat_interval_seconds))
    ttl_seconds = max(heartbeat_interval_seconds * 2, int(ttl_seconds))
    _remove_workspace_from_sys_path(workspace)

    if _release_if_profile_quiet(task_id):
        return 0

    proc = subprocess.Popen(  # noqa: S603 -- command is dispatcher-constructed
        list(command),
        cwd=workspace if workspace and os.path.isdir(workspace) else None,
        stdin=subprocess.DEVNULL,
        env=_worker_env(workspace),
        start_new_session=True,
    )
    started_at = time.time()

    stopping = False

    def _handle_stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        _terminate_child(proc)

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_stop)
            except Exception:
                pass

    last_hb = 0.0
    while proc.poll() is None and not stopping:
        now = time.time()
        if now - last_hb >= heartbeat_interval_seconds:
            last_hb = now
            _heartbeat(task_id, ttl_seconds, "worker supervisor heartbeat")
        guard_reason = _materials_token_guard_reason(task_id, log_path, started_at)
        if guard_reason:
            _block_task(task_id, guard_reason)
            _terminate_child(proc)
            return 0
        time.sleep(2)

    rc = proc.poll()
    if rc is None:
        _terminate_child(proc)
        rc = proc.poll()
    if stopping:
        if _task_is_running(task_id):
            if _record_child_exit(
                task_id=task_id,
                returncode=rc,
                log_path=log_path,
                command=command,
                started_at=started_at,
                reason="worker_supervisor_stopped",
            ):
                return 0
        return int(rc or 143)

    if _task_is_running(task_id) and _release_if_budget_exhausted(task_id, log_path):
        return 0
    if _task_is_running(task_id):
        tail = _tail_text(log_path)
        provider_failure_markers = (
            "provider_empty_response",
            "provider_rate_limited",
            "provider_protocol_error",
        )
        provider_failure = next(
            (marker for marker in provider_failure_markers if marker in tail), None
        )
        if provider_failure:
            if _record_child_exit(
                task_id=task_id,
                returncode=rc,
                log_path=log_path,
                command=command,
                started_at=started_at,
                reason=provider_failure,
            ):
                return 0
        if _record_child_exit(
            task_id=task_id,
            returncode=rc,
            log_path=log_path,
            command=command,
            started_at=started_at,
        ):
            return 0
    return int(rc or 0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--ttl", type=int, required=True)
    parser.add_argument("--heartbeat-interval", type=int, required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--workspace", default="")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    return run_supervisor(
        task_id=args.task_id,
        ttl_seconds=args.ttl,
        heartbeat_interval_seconds=args.heartbeat_interval,
        log_path=Path(args.log_path),
        workspace=args.workspace,
        command=command,
    )


if __name__ == "__main__":
    raise SystemExit(main())
