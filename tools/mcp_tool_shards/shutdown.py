"""Executable source shard for the legacy MCP tool seam.

The source is compiled with the original module namespace so public
imports and monkeypatch targets remain tools.mcp_tool-compatible.
"""
import linecache
from pathlib import Path

_SOURCE = r'''


def _kill_orphaned_mcp_children(
    include_active: bool = False,
    server_name: Optional[str] = None,
) -> None:
    """Best-effort graceful shutdown of stdio MCP subprocesses to reap orphans.

    Orphans are PIDs that survived their session context exit (SDK teardown
    did not terminate the process — common on Linux when stdio children escape
    the parent cgroup on cancellation). By default only entries in
    ``_orphan_stdio_pids`` are reaped so concurrent cron jobs and live user
    sessions are not disrupted.

    Sends SIGTERM, waits 2 seconds, then escalates to SIGKILL for any
    survivors, avoiding shared-resource collisions when multiple hermes
    processes run on the same host (each has its own ``_stdio_pids`` dict).

    On POSIX, signals are sent via ``os.killpg`` to the spawn-time pgid when
    one is tracked, so reparented grandchildren in the same process group
    (e.g. ``claude mcp serve`` spawned by a stdio MCP wrapper that exited
    first) are reaped alongside the direct child.  Falls back to ``os.kill``
    on Windows and when no pgid is recorded.

    When ``server_name`` is set, only orphaned PIDs known to belong to that
    MCP server are reaped. This lets stdio reconnects clean up their previous
    transport without touching unrelated servers.

    With ``include_active=True`` also kills every PID in ``_stdio_pids`` —
    used only at final shutdown, after the MCP event loop has stopped and no
    sessions can still be in flight.
    """
    import signal as _signal

    with _lock:
        pids: Dict[int, str] = {}
        for opid in _orphan_stdio_pids:
            owner = _orphan_stdio_pid_servers.get(opid, "orphan")
            if server_name is not None and owner != server_name:
                continue
            pids[opid] = owner
        for opid in pids:
            _orphan_stdio_pids.discard(opid)
            _orphan_stdio_pid_servers.pop(opid, None)
        if include_active:
            active = dict(_stdio_pids)
            if server_name is not None:
                active = {
                    pid: owner
                    for pid, owner in active.items()
                    if owner == server_name
                }
            pids.update(active)
            for pid in active:
                _stdio_pids.pop(pid, None)
        # Snapshot pgids for the pids we're about to kill, then drop the
        # entries so a future spawn can't collide with stale state.
        pgids: Dict[int, int] = {pid: _stdio_pgids[pid] for pid in pids if pid in _stdio_pgids}
        for pid in pgids:
            _stdio_pgids.pop(pid, None)

    # Fast path: no tracked stdio PIDs to reap. Skip the SIGTERM/sleep/SIGKILL
    # dance entirely — otherwise every MCP-free shutdown pays a 2s sleep tax.
    if not pids:
        return

    # Pre-compute the gateway's own pgid so _send_signal can avoid killing it.
    try:
        _my_pgid = os.getpgrp()
    except (AttributeError, OSError):
        _my_pgid = None  # Windows or restricted environment

    def _send_signal(pid: int, sig: int, server_name: str) -> None:
        """SIGTERM/SIGKILL via pgroup on POSIX, fall back to pid signal."""
        pgid = pgids.get(pid)
        killpg = getattr(os, "killpg", None)
        if pgid is not None and killpg is not None:
            if _my_pgid is not None and pgid == _my_pgid:
                # The MCP child shares the gateway's own process group.
                # Using killpg would deliver the signal to the gateway as
                # well, crashing it (see #47134).  Fall through to the
                # per-pid kill() path instead. Warn because per-pid kill
                # cannot reach grandchildren in this shared group — if the
                # direct child has already exited, they may leak (inherent:
                # group-killing them would also kill the gateway).
                logger.warning(
                    "MCP server '%s' pgid %d matches gateway pgid; skipping "
                    "killpg to avoid self-kill and using per-pid kill — any "
                    "grandchildren in this group may not be reaped",
                    server_name, pgid,
                )
            else:
                try:
                    killpg(pgid, sig)
                    return
                except (ProcessLookupError, PermissionError, OSError) as exc:
                    # Pgroup gone (all members exited) or refused — fall back to
                    # the per-pid path so we still try the direct child if alive.
                    logger.debug(
                        "killpg(%d, %d) failed for MCP server '%s': %s; falling back to kill(pid)",
                        pgid, sig, server_name, exc,
                    )
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # Phase 1: SIGTERM (graceful)
    for pid, server_name in pids.items():
        _send_signal(pid, _signal.SIGTERM, server_name)
        logger.debug("Sent SIGTERM to orphaned MCP process %d (%s)", pid, server_name)

    # Phase 2: Wait for graceful exit
    time.sleep(2)

    # Phase 3: SIGKILL any survivors
    _sigkill = getattr(_signal, "SIGKILL", _signal.SIGTERM)
    # ``os.kill(pid, 0)`` is NOT a no-op on Windows. Use the cross-platform
    # existence check before escalating to SIGKILL.
    from gateway.status import _pid_exists
    for pid, server_name in pids.items():
        if not _pid_exists(pid):
            continue  # Good — exited after SIGTERM
        _send_signal(pid, _sigkill, server_name)
        logger.warning(
            "Force-killed MCP process %d (%s) after SIGTERM timeout",
            pid, server_name,
        )


def _stop_mcp_loop_if_idle() -> bool:
    """Stop the MCP loop only when no registered server still owns it.

    Probe paths create temporary MCPServerTask instances that are not placed in
    ``_servers``.  They should clean up an otherwise-idle loop, but must not
    tear down the process-global loop when live agent tools are registered on
    it.  Otherwise a dashboard/CLI probe can make later MCP tool calls fail
    with ``MCP event loop is not running``.
    """
    return _stop_mcp_loop(only_if_idle=True)


async def _drain_mcp_loop_tasks(
    *,
    timeout: float = _MCP_LOOP_DRAIN_TIMEOUT,
) -> None:
    """Cancel every task still pending on the MCP loop and reap it.

    Cancelling is not enough on its own: ``Task.cancel()`` only schedules the
    throw, so tasks need a cancellation cycle before the loop goes away. Wait
    for them here — on their owning loop — but keep the final drain bounded so
    a task that suppresses cancellation cannot hang process exit indefinitely.
    """
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if not pending:
        return
    logger.debug("Draining %d pending task(s) from the MCP loop", len(pending))
    for task in pending:
        task.cancel()

    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in done:
        if task.cancelled():
            continue
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Pending MCP loop task ended during shutdown: %s", exc)

    if still_pending:
        logger.warning(
            "%d MCP loop task(s) still pending after %.1fs drain",
            len(still_pending), timeout,
        )


async def _drain_and_stop_mcp_loop() -> None:
    """Drain pending tasks, then stop the loop from its owning thread.

    Keeping both operations in one loop-owned sequence matters when the caller
    times out waiting for a blocked loop. Queuing ``loop.stop`` separately from
    the caller can overtake the scheduled drain before it receives a loop cycle,
    leaving the drain coroutine itself pending when the loop is closed.
    """
    loop = asyncio.get_running_loop()
    try:
        await _drain_mcp_loop_tasks(timeout=_MCP_LOOP_DRAIN_TIMEOUT)
    finally:
        loop.call_soon(loop.stop)


def _stop_mcp_loop(*, only_if_idle: bool = False) -> bool:
    """Stop the background event loop and join its thread."""
    global _mcp_loop, _mcp_thread
    with _lock:
        if only_if_idle and (_servers or _server_connecting):
            logger.debug("Leaving MCP event loop running; active servers are registered or connecting")
            return False
        loop = _mcp_loop
        thread = _mcp_thread
        _mcp_loop = None
        _mcp_thread = None
    if loop is not None:
        # Drain before stopping: closing the loop with tasks still suspended
        # leaves their coroutines for the GC, whose finalizer then resumes them
        # to run cleanup against a loop that is already closed -> "Event loop
        # is closed" (#60197). ``shutdown_mcp_servers`` only reaps servers held
        # in ``_servers``, so anything else left on this loop ends up here.
        stop_owned_by_loop = False
        if loop.is_running():
            from agent.async_utils import safe_schedule_threadsafe

            future = safe_schedule_threadsafe(
                _drain_and_stop_mcp_loop(), loop,
                logger=logger,
                log_message="MCP loop drain: failed to schedule",
                log_level=logging.WARNING,
            )
            if future is not None:
                stop_owned_by_loop = True
                try:
                    future.result(timeout=_MCP_LOOP_DRAIN_TIMEOUT + 1)
                except TimeoutError:
                    logger.warning(
                        "Timed out waiting for MCP loop drain after %.1fs",
                        _MCP_LOOP_DRAIN_TIMEOUT + 1,
                    )
                except BaseException as exc:
                    logger.warning("Error draining MCP loop tasks: %s", exc)
        elif not loop.is_closed():
            try:
                loop.run_until_complete(
                    _drain_mcp_loop_tasks(timeout=_MCP_LOOP_DRAIN_TIMEOUT)
                )
            except BaseException as exc:
                logger.warning("Error draining stopped MCP loop tasks: %s", exc)

        if not stop_owned_by_loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("MCP event loop thread did not stop within 5.0s")
        try:
            loop.close()
        except Exception as exc:
            logger.warning("Unable to close MCP event loop cleanly: %s", exc)
        # After closing the loop, any stdio subprocesses that survived the
        # graceful shutdown are now orphaned — include active PIDs too
        # since the loop is gone and no session can still be in flight.
        _kill_orphaned_mcp_children(include_active=True)
    return True
'''

EXPORTED_NAMES = ('_kill_orphaned_mcp_children', '_stop_mcp_loop_if_idle', '_drain_mcp_loop_tasks', '_drain_and_stop_mcp_loop', '_stop_mcp_loop')
SOURCE_PATH = Path(__file__)

def install(namespace: dict[str, object]) -> None:
    filename = str(SOURCE_PATH)
    linecache.cache[filename] = (
        len(_SOURCE), None, _SOURCE.splitlines(True), filename
    )
    exec(compile(_SOURCE, filename, "exec"), namespace, namespace)
