"""Executable source shard for the legacy MCP tool seam.

The source is compiled with the original module namespace so public
imports and monkeypatch targets remain tools.mcp_tool-compatible.
"""
import linecache
from pathlib import Path

_SOURCE = r'''


def _get_auth_error_types() -> tuple:
    """Return a tuple of exception types that indicate MCP OAuth failure.

    Cached after first call. Includes:
      - ``mcp.client.auth.OAuthFlowError`` / ``OAuthTokenError`` — raised by
        the SDK's auth flow when discovery, refresh, or full re-auth fails.
      - ``mcp.client.auth.UnauthorizedError`` (older MCP SDKs) — kept as an
        optional import for forward/backward compatibility.
      - ``tools.mcp_oauth.OAuthNonInteractiveError`` — raised by our callback
        handler when no user is present to complete a browser flow.
      - ``HTTPStatusError`` from both httpx flavours — caller must
        additionally check ``status_code == 401`` via :func:`_is_auth_error`.
    """
    global _AUTH_ERROR_TYPES
    if _AUTH_ERROR_TYPES:
        return _AUTH_ERROR_TYPES
    types: list = []
    try:
        from mcp.client.auth import OAuthFlowError, OAuthTokenError
        types.extend([OAuthFlowError, OAuthTokenError])
    except ImportError:
        pass
    try:
        # Older MCP SDK variants exported this
        from mcp.client.auth import UnauthorizedError  # type: ignore
        types.append(UnauthorizedError)
    except ImportError:
        pass
    try:
        from tools.mcp_oauth import OAuthNonInteractiveError
        types.append(OAuthNonInteractiveError)
    except ImportError:
        pass
    types.extend(_http_status_error_types())
    _AUTH_ERROR_TYPES = tuple(types)
    return _AUTH_ERROR_TYPES


def _is_auth_error(exc: BaseException) -> bool:
    """Return True if ``exc`` indicates an MCP OAuth failure.

    ``HTTPStatusError`` is only treated as auth-related when the response
    status code is 401. Other HTTP errors fall through to the generic error
    path in the tool handlers.
    """
    types = _get_auth_error_types()
    if not types or not isinstance(exc, types):
        return False
    status_error_types = _http_status_error_types()
    if status_error_types and isinstance(exc, status_error_types):
        return getattr(exc.response, "status_code", None) == 401
    return True


def _handle_auth_error_and_retry(
    server_name: str,
    exc: BaseException,
    retry_call,
    op_description: str,
):
    """Attempt auth recovery and one retry; return None to fall through.

    Called by the 5 MCP tool handlers when ``session.<op>()`` raises an
    auth-related exception. Workflow:

      1. Ask :class:`tools.mcp_oauth_manager.MCPOAuthManager.handle_401` if
         recovery is viable (i.e., disk has fresh tokens, or the SDK can
         refresh in-place).
      2. If yes, set the server's ``_reconnect_event`` so the server task
         tears down the current MCP session and rebuilds it with fresh
         credentials. Wait briefly for ``_ready`` to re-fire.
      3. Retry the operation once. Return the retry result if it produced
         a non-error JSON payload. Otherwise return the ``needs_reauth``
         error dict so the model stops hallucinating manual refresh.
      4. Return None if ``exc`` is not an auth error, signalling the
         caller to use the generic error path.

    Args:
        server_name: Name of the MCP server that raised.
        exc: The exception from the failed tool call.
        retry_call: Zero-arg callable that re-runs the tool call, returning
            the same JSON string format as the handler.
        op_description: Human-readable name of the operation (for logs).

    Returns:
        A JSON string if auth recovery was attempted, or None to fall
        through to the caller's generic error path.
    """
    if not _is_auth_error(exc):
        return None

    from tools.mcp_oauth_manager import get_manager
    manager = get_manager()

    async def _recover():
        return await manager.handle_401(server_name, None)

    try:
        recovered = _run_on_mcp_loop(_recover, timeout=10)
    except Exception as rec_exc:
        logger.warning(
            "MCP OAuth '%s': recovery attempt failed: %s",
            server_name, rec_exc,
        )
        recovered = False

    if recovered:
        with _lock:
            srv = _servers.get(server_name)
        reconnected = False
        if srv is not None and hasattr(srv, "_reconnect_event"):
            reconnected = _signal_reconnect_and_wait(
                server_name,
                srv,
                op_description=f"{op_description} after OAuth recovery",
                timeout=15,
            )

        # A successful OAuth recovery + transport reconnect is independent
        # evidence that the server is viable again, so close the circuit
        # breaker here — not only on retry success. Without this, a reconnect
        # followed by a failing retry would leave the breaker pinned above
        # threshold forever. The post-reset retry still goes through
        # _bump_server_error on failure, so a genuinely broken server will
        # re-trip the breaker as normal.
        if reconnected:
            _reset_server_error(server_name)

        try:
            result = retry_call()
            try:
                parsed = json.loads(result)
                if "error" not in parsed:
                    _reset_server_error(server_name)
                    return result
            except (json.JSONDecodeError, TypeError):
                _reset_server_error(server_name)
                return result
        except Exception as retry_exc:
            logger.warning(
                "MCP %s/%s retry after auth recovery failed: %s",
                server_name, op_description, retry_exc,
            )

    # No recovery available, or retry also failed: surface a structured
    # needs_reauth error. Bumps the circuit breaker so the model stops
    # retrying the tool.
    _bump_server_error(server_name)
    return tool_error(
        f"MCP server '{server_name}' requires re-authentication. "
        f"Run `hermes mcp login {server_name}` (or delete the tokens "
        f"file under ~/.hermes/mcp-tokens/ and restart). Do NOT retry "
        f"this tool — ask the user to re-authenticate.",
        needs_reauth=True,
        server=server_name,
    )


# Substrings (lower-cased match) that indicate the MCP server rejected
# the request because its server-side transport session expired /
# was garbage-collected.  The caller's OAuth token is still valid —
# only the transport-layer session state needs rebuilding.  See #13383.
_SESSION_EXPIRED_MARKERS: tuple = (
    "invalid or expired session",
    "expired session",
    "session expired",
    "session not found",
    "unknown session",
    "session terminated",
    "closedresourceerror",
    "closed resource",
    "transport is closed",
    "connection closed",
    "broken pipe",
    "end of file",
)

# Upper bound on exception-graph nodes inspected by
# ``_is_session_expired_error``. The visited-identity set already breaks
# cycles across ``exceptions`` / ``__cause__`` / ``__context__``; the
# budget additionally bounds pathological acyclic graphs (e.g. deeply
# chained retries) so classification always terminates promptly. Kept
# comfortably above ``sys.getrecursionlimit()`` so legitimately deep
# wrapper stacks (task-group nesting) are still fully scanned.
_EXC_TRAVERSAL_MAX_NODES = 10_000


def _is_session_expired_error(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like an MCP transport session expiry.

    Streamable HTTP MCP servers may garbage-collect server-side session
    state while the OAuth token remains valid — idle TTL, server
    restart, horizontal-scaling pod rotation, etc.  The SDK surfaces
    this as a JSON-RPC error whose message contains phrases like
    ``"Invalid or expired session"``.  This class of failure is
    distinct from :func:`_is_auth_error`: re-running the OAuth refresh
    flow would be pointless because the access token is fine.  What's
    needed is a transport reconnect — tear down and rebuild the
    ``streamablehttp_client`` + ``ClientSession`` pair, which is
    exactly what ``MCPServerTask._reconnect_event`` triggers.
    """
    # AnyIO's stream exceptions are commonly message-less. In particular,
    # ``str(ClosedResourceError()) == ""``, so marker matching alone misses the
    # exact failure emitted by both MCP stdio and HTTP transports.
    try:
        from anyio import BrokenResourceError, ClosedResourceError, EndOfStream

        transport_error_types = (
            BrokenResourceError,
            ClosedResourceError,
            EndOfStream,
        )
    except ImportError:  # pragma: no cover - AnyIO is supplied by the MCP SDK
        transport_error_types = ()

    # ExceptionGroup trees can be arbitrarily deep or even cyclic when custom
    # exceptions expose ``exceptions``, and chained exceptions
    # (``raise X from Y`` / implicit ``__context__``) can likewise form
    # cycles when handlers re-raise previously seen exceptions. Traverse
    # once, iteratively, with an identity-visited set AND a bounded node
    # budget so classification can never spin, and inspect every reachable
    # node so user interruption always overrides transport markers or types
    # found elsewhere in the graph. The chain traversal matters for real
    # failures: SDK wrappers often raise a generic RuntimeError *from* the
    # message-less ClosedResourceError, leaving the transport signal only
    # reachable via ``__cause__``.
    stack: "list[BaseException | None]" = [exc]
    seen: set[int] = set()
    transport_error_found = False
    budget = _EXC_TRAVERSAL_MAX_NODES
    while stack and budget > 0:
        current = stack.pop()
        if current is None:
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        budget -= 1

        if isinstance(current, InterruptedError):
            return False
        if isinstance(current, transport_error_types):
            transport_error_found = True

        # Exception messages vary across SDK versions + server
        # implementations, so match on a small allow-list of stable
        # substrings rather than exception type. Kept narrow to avoid
        # false positives on unrelated server errors.
        msg = str(current).lower()
        if msg and any(marker in msg for marker in _SESSION_EXPIRED_MARKERS):
            transport_error_found = True

        stack.extend(getattr(current, "exceptions", ()))
        stack.append(getattr(current, "__cause__", None))
        stack.append(getattr(current, "__context__", None))

    return transport_error_found



def _handle_session_expired_and_retry(
    server_name: str,
    exc: BaseException,
    retry_call,
    op_description: str,
):
    """Trigger a transport reconnect and retry once on session expiry.

    Unlike :func:`_handle_auth_error_and_retry`, this does **not** call
    the OAuth manager's ``handle_401`` — the access token is still
    valid, only the server-side session state is stale.  Setting
    ``_reconnect_event`` causes the server task's lifecycle loop to
    tear down the current ``streamablehttp_client`` + ``ClientSession``
    and rebuild them, reusing the existing OAuth provider instance.
    See #13383.

    Args:
        server_name: Name of the MCP server that raised.
        exc: The exception from the failed call.
        retry_call: Zero-arg callable that re-runs the operation,
            returning the same JSON string format as the handler.
        op_description: Human-readable name of the operation (logs).

    Returns:
        A JSON string if reconnect + retry was attempted and produced
        a response, or ``None`` to fall through to the caller's
        generic error path (not a session-expired error, no server
        record, reconnect didn't ready in time, or retry also failed).
    """
    if not _is_session_expired_error(exc):
        return None

    with _lock:
        srv = _servers.get(server_name)
    if srv is None or not hasattr(srv, "_reconnect_event"):
        return None

    loop = _mcp_loop
    if loop is None or not loop.is_running():
        return None

    logger.info(
        "MCP server '%s': %s failed with session-expired error (%s); "
        "signalling transport reconnect and retrying once.",
        server_name, op_description, exc,
    )

    # Trigger the same reconnect mechanism the OAuth recovery path
    # uses, then wait briefly for the new session to come back ready.
    if not _signal_reconnect_and_wait(
        server_name,
        srv,
        op_description=op_description,
        timeout=15,
    ):
        logger.warning(
            "MCP server '%s': reconnect did not ready within 15s after "
            "session-expired error; falling through to error response.",
            server_name,
        )
        return None

    try:
        result = retry_call()
        try:
            parsed = json.loads(result)
            if "error" not in parsed:
                _reset_server_error(server_name)
                return result
        except (json.JSONDecodeError, TypeError):
            _reset_server_error(server_name)
            return result
    except Exception as retry_exc:
        logger.warning(
            "MCP %s/%s retry after session reconnect failed: %s",
            server_name, op_description, retry_exc,
        )
    return None


# Exact raw server names whose ``supports_parallel_tool_calls`` config is True.
# Raw identity matters: distinct names such as ``foo-bar`` and ``foo_bar`` both
# sanitize to ``foo_bar`` but must not share policy.
_parallel_safe_servers: set = set()

# Exact MCP tool-name provenance. The generated registry name is lossy because
# provider-safe normalization maps punctuation to ``_``. Keep the raw server
# name captured at registration time so policy and capability checks never rely
# on parsing or re-sanitizing the generated name.
_mcp_tool_server_names: Dict[str, str] = {}

# Dedicated event loop running in a background daemon thread.
_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_thread: Optional[threading.Thread] = None

# Protects _mcp_loop, _mcp_thread, _servers, MCP connection status maps,
# _parallel_safe_servers, _mcp_tool_server_names, and _stdio_pids.
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Cross-process MCP discovery guard
# ---------------------------------------------------------------------------
# Advisory file lock that prevents N concurrent Hermes processes (e.g.
# gateway + CLI + TUI) from all running MCP discovery simultaneously.
# See issue #62771.
_LOCK_UNAVAILABLE: Any = object()  # sentinel: locking broken/unavailable
_MCP_DISCOVERY_LOCK_PATH: Optional[str] = None  # resolved lazily

# Retry constants for the bounded wait when another process holds the lock.
_MCP_DISCOVERY_LOCK_MAX_RETRIES: int = 240
_MCP_DISCOVERY_LOCK_RETRY_DELAY_S: float = 0.5


class _LockCookie:
    """Holds a cross-process file lock; release() drops it.

    On Windows the underlying file handle MUST stay alive while the lock is
    held (portalocker keeps the kernel lock on the fd).  On POSIX the fcntl
    lockdown is similarly tied to the file-descriptor lifetime.  We keep the
    file object in ``_fh`` and close it on release.
    """

    def __init__(self, fh: Any) -> None:
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                fd = self._fh.fileno()
                if os.name == "posix":
                    import fcntl
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                else:
                    import portalocker
                    try:
                        portalocker.unlock(self._fh)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def _acquire_lock_on_fh(fh: Any) -> bool:
    """Acquire a non-blocking exclusive lock on an open file handle.

    Uses ``fcntl.flock`` on POSIX and ``portalocker.lock`` on Windows.

    Returns ``True`` if the lock was acquired, ``False`` if another process
    holds it (non-blocking refusal).  Raises ``RuntimeError`` on unexpected
    errors so the caller can treat lock acquisition as unavailable.
    """
    fd = fh.fileno()
    if os.name == "posix":
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise
    else:
        import portalocker
        try:
            portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
            return True
        except portalocker.LockException:
            return False


def _try_acquire_mcp_discovery_lock() -> Any:
    """Try to acquire an exclusive cross-process lock for MCP discovery.

    Returns
    -------
    _LockCookie
        Lock acquired successfully.
    None
        Another process holds the lock (non-blocking refusal).
    _LOCK_UNAVAILABLE
        Locking mechanism is broken or unavailable -- caller should run
        discovery unguarded.
    """
    global _MCP_DISCOVERY_LOCK_PATH
    try:
        from hermes_constants import get_hermes_home
        if _MCP_DISCOVERY_LOCK_PATH is None:
            _MCP_DISCOVERY_LOCK_PATH = str(
                get_hermes_home() / ".mcp-discovery.lock"
            )
        lock_path = _MCP_DISCOVERY_LOCK_PATH
    except Exception:
        return _LOCK_UNAVAILABLE

    try:
        fh = open(lock_path, "w", encoding="utf-8")
    except Exception:
        return _LOCK_UNAVAILABLE

    try:
        acquired = _acquire_lock_on_fh(fh)
    except Exception:
        fh.close()
        return _LOCK_UNAVAILABLE

    if acquired:
        return _LockCookie(fh)
    else:
        fh.close()
        return None


# PIDs of stdio MCP server subprocesses.  Tracked so we can force-kill
# them on shutdown if the graceful cleanup (SDK context-manager teardown)
# fails or times out.  PIDs are added after connection and removed on
# normal server shutdown.
_stdio_pids: Dict[int, str] = {}  # pid -> server_name

# PIDs that survived their session context exit (SDK teardown failed to
# terminate them).  These are detected in _run_stdio's finally block and
# can be cleaned up asynchronously by _kill_orphaned_mcp_children().
# Separate from _stdio_pids so cleanup sweeps never race with active
# sessions (e.g. concurrent cron jobs or live user chats).
_orphan_stdio_pids: set = set()
_orphan_stdio_pid_servers: Dict[int, str] = {}

# Process-group IDs of stdio MCP subprocesses, captured at spawn time.
# The MCP SDK spawns stdio children with ``start_new_session=True`` so each
# direct child becomes its own session/pgroup leader (PGID == its own PID).
# Grandchildren spawned by that child (e.g. a wrapper MCP server that itself
# launches helper subprocesses like ``claude mcp serve``) inherit that PGID
# unless they call ``setsid`` themselves.  When the direct child exits, those
# grandchildren reparent to init/systemd-user but keep the original PGID, so
# ``killpg(pgid, sig)`` still reaches them.  Tracked separately from
# ``_stdio_pids`` so we retain the PGID even after the direct child has
# exited and been removed from the active map.  Empty on Windows
# (``os.getpgid`` is POSIX-only).
_stdio_pgids: Dict[int, int] = {}  # pid -> pgid


def _snapshot_child_pids() -> set:
    """Return a set of current child process PIDs.

    Uses /proc on Linux, falls back to psutil, then empty set.
    Used by _run_stdio to identify the subprocess spawned by stdio_client.
    """
    my_pid = os.getpid()

    # Linux: read from /proc
    try:
        children_path = f"/proc/{my_pid}/task/{my_pid}/children"
        with open(children_path, encoding="utf-8") as f:
            return {int(p) for p in f.read().split() if p.strip()}
    except (FileNotFoundError, OSError, ValueError):
        pass

    # Fallback: psutil
    try:
        import psutil
        return {c.pid for c in psutil.Process(my_pid).children()}
    except Exception:
        pass

    return set()


# Non-MCP gateway children that can race into the _snapshot_child_pids() delta
# during stdio MCP server spawn. LSP servers and slash_worker now use
# start_new_session=True too; this remains defense-in-depth for any future
# non-MCP child spawn that briefly appears in the MCP snapshot delta. Match
# argv markers instead of argv[0] because Python/Java children begin with the
# interpreter or binary path.
_NON_MCP_CHILD_CMDLINE_MARKERS: tuple[str, ...] = (
    "tui_gateway.slash_worker",
    "tui_gateway.entry",
    "-dorg.eclipse.equinox.launcher",  # jdtls (legacy arg style)
    "eclipse.jdt.ls",
    "org.eclipse.equinox.launcher_",
)


def _filter_mcp_children(pids: set) -> set:
    """Remove non-MCP children from a PID snapshot delta.

    _snapshot_child_pids() returns *all* direct children of the gateway. When
    a stdio MCP server spawns concurrently with a slash_worker or LSP server
    spawn, the delta ``_snapshot_child_pids() - pids_before`` can include
    PIDs that are NOT the MCP server. Tracking those PIDs in _stdio_pgids is
    catastrophic if a future child lacks start_new_session: its pgid can be the
    TUI parent's PID, so the shutdown sweep's killpg() kills the TUI itself.
    """
    if not pids:
        return pids
    try:
        import psutil
    except ImportError:
        # psutil unavailable — keep all PIDs (preserves prior behavior).
        return pids
    filtered: set = set()
    for pid in pids:
        try:
            argv = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            # Process raced away or is a zombie — skip it; it cannot be the
            # MCP server we just spawned and is not safe to track.
            continue
        if any(
            marker in arg
            for arg in argv[1:]
            for marker in _NON_MCP_CHILD_CMDLINE_MARKERS
        ):
            continue
        filtered.add(pid)
    return filtered


def _mcp_loop_exception_handler(loop, context):
    """Suppress benign 'Event loop is closed' noise during shutdown.

    When the MCP event loop is stopped and closed, httpx/httpcore async
    transports may fire __del__ finalizers that call call_soon() on the
    dead loop.  asyncio catches that RuntimeError and routes it here.
    We silence it because the connection is being torn down anyway; all
    other exceptions are forwarded to the default handler.
    """
    exc = context.get("exception")
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        return  # benign shutdown race — suppress
    loop.default_exception_handler(context)


def _ensure_mcp_loop():
    """Start the background event loop thread if not already running."""
    global _mcp_loop, _mcp_thread
    with _lock:
        if _mcp_loop is not None and _mcp_loop.is_running():
            return
        _mcp_loop = asyncio.new_event_loop()
        _mcp_loop.set_exception_handler(_mcp_loop_exception_handler)
        _mcp_thread = threading.Thread(
            target=_mcp_loop.run_forever,
            name="mcp-event-loop",
            daemon=True,
        )
        _mcp_thread.start()


def _wrap_with_home_override(coro: "Coroutine") -> "Coroutine":
    """Carry the caller's context-local HERMES_HOME override into ``coro``.

    Returns ``coro`` unchanged when no override is active. Otherwise wraps
    it so the override is set inside the coroutine's own (task-local)
    context on the MCP loop and reset when it completes — concurrent calls
    carrying different scopes don't interfere.
    """
    try:
        from hermes_constants import (
            get_hermes_home_override,
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        home_override = get_hermes_home_override()
    except Exception:
        return coro
    if not home_override:
        return coro

    async def _scoped():
        token = set_hermes_home_override(home_override)
        try:
            return await coro
        finally:
            reset_hermes_home_override(token)

    return _scoped()


def _wrap_with_dashboard_oauth_flow(coro):
    """Propagate a dashboard OAuth flow onto the dedicated MCP loop task."""
    try:
        from tools.mcp_dashboard_oauth import (
            dashboard_oauth_flow,
            get_dashboard_oauth_flow,
        )

        flow = get_dashboard_oauth_flow()
    except Exception:
        return coro
    if flow is None:
        return coro

    async def _scoped():
        with dashboard_oauth_flow(flow):
            return await coro

    return _scoped()


def _run_on_mcp_loop(coro_or_factory, timeout: float = 30):
    """Schedule a coroutine on the MCP event loop and block until done.

    Accepts either a coroutine object or a zero-arg callable that returns one.
    Callers can pass a factory to avoid constructing coroutine objects when
    the MCP loop is unavailable (which would otherwise leak the coroutine
    frame and emit ``"coroutine was never awaited"`` warnings).

    Poll in short intervals so the calling agent thread can honor user
    interrupts while the MCP work is still running on the background loop.
    """
    from tools.interrupt import is_interrupted
    from agent.async_utils import safe_schedule_threadsafe

    with _lock:
        loop = _mcp_loop
    if loop is None or not loop.is_running():
        if asyncio.iscoroutine(coro_or_factory):
            coro_or_factory.close()
        raise RuntimeError("MCP event loop is not running")

    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory

    # Propagate the context-local HERMES_HOME override onto the MCP loop.
    # Tasks scheduled via run_coroutine_threadsafe are created INSIDE the
    # loop thread, so they copy the loop thread's context — not the
    # scheduling thread's. A per-request profile scope (the dashboard's
    # ?profile= endpoints, e.g. the MCP "Test server" probe) would silently
    # vanish here: OAuth token stores and any other get_hermes_home()
    # resolution inside the coroutine would read the process home instead
    # of the selected profile's. Re-establish the override inside the
    # task's own context (task-local — concurrent calls carrying different
    # scopes don't interfere). No-op when no override is active.
    coro = _wrap_with_home_override(coro)
    coro = _wrap_with_dashboard_oauth_flow(coro)

    future = safe_schedule_threadsafe(
        coro, loop,
        logger=logger,
        log_message="MCP scheduling failed",
    )
    if future is None:
        raise RuntimeError("MCP event loop unavailable (failed to schedule)")
    start_time = time.monotonic()
    deadline = None if timeout is None else start_time + timeout

    while True:
        if is_interrupted():
            future.cancel()
            raise InterruptedError("User sent a new message")

        wait_timeout = 0.1
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                elapsed = time.monotonic() - start_time
                raise TimeoutError(
                    f"MCP call timed out after {elapsed:.1f}s "
                    f"(configured timeout: {float(timeout):.1f}s)"
                )
            wait_timeout = min(wait_timeout, remaining)

        try:
            return future.result(timeout=wait_timeout)
        except concurrent.futures.TimeoutError:
            # On supported Python versions, concurrent.futures.TimeoutError
            # aliases the built-in TimeoutError, so result(timeout=...) also
            # raises it for a coroutine's own timeout.
            # Resolve a done future without a timeout to propagate its stored
            # outcome, including completion racing with this polling timeout.
            if future.done():
                return future.result()
            continue


def _interrupted_call_result() -> str:
    """Standardized JSON error for a user-interrupted MCP tool call."""
    return tool_error("MCP call interrupted: user sent a new message")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _interpolate_env_vars(value):
    """Recursively resolve ``${VAR}`` placeholders.

    Both ``${VAR}`` and Cursor-style ``${env:VAR}`` are accepted — the
    ``env:`` prefix is stripped so a doc copied from a Cursor / Claude MCP
    config resolves the same secret. Cursor's context variables are also
    supported (case-sensitive): ``${userHome}``, ``${workspaceFolder}``,
    ``${workspaceFolderBasename}``, ``${pathSeparator}`` and ``${/}`` — see
    :func:`_context_var_value` / :func:`_workspace_folder` for resolution.
    Env refs resolve from the active profile's secret scope when multiplexing
    is on (so an MCP server config's ``${API_KEY}`` picks up the routed
    profile's value, not the process-global ``os.environ`` which may hold
    another profile's), falling back to ``os.environ`` otherwise. Unset vars
    keep the literal placeholder, as before.
    """
    from agent.secret_scope import get_secret as _get_secret

    if isinstance(value, str):
        def _replace(m):
            ctx = _context_var_value(m.group(1).strip())
            if ctx is not None:
                return ctx
            name = _env_ref_name(m.group(1))
            return _get_secret(name, m.group(0)) or m.group(0)
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env_vars(v) for v in value]
    return value


# (server_name, dotted key path) pairs already warned about — see
# _warn_hidden_whitespace(); config loads happen on every discovery pass.
_whitespace_warned: Set[Tuple[str, str]] = set()


def _warn_hidden_whitespace(server_name: str, config: dict) -> List[str]:
    """Warn about MCP config string values with hidden leading/trailing whitespace.

    A token pasted with a trailing newline or a URL copied with a leading
    space produces opaque auth/connect failures (the server rejects the
    credential, TLS/DNS fails on ``"example.com "``), and the whitespace is
    invisible when eyeballing config.yaml. Inspired by Claude Code v2.1.219,
    which added the same startup warning for its MCP config values.

    Advisory only — values are never mutated (whitespace could theoretically
    be intentional in an arg). Returns the list of dotted key paths flagged,
    for testability. Values themselves are never logged (they are often
    secrets); only the key path is named. Each (server, key path) is warned
    about once per process — ``_load_mcp_config()`` runs on every discovery/
    status call and repeating the warning would be noise.
    """
    flagged: List[str] = []

    def _walk(value: Any, path: str) -> None:
        if isinstance(value, str):
            if value != value.strip():
                flagged.append(path)
        elif isinstance(value, dict):
            for k, v in value.items():
                _walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _walk(v, f"{path}[{i}]")

    _walk(config, "")
    for key_path in flagged:
        dedupe_key = (server_name, key_path)
        if dedupe_key in _whitespace_warned:
            continue
        _whitespace_warned.add(dedupe_key)
        logger.warning(
            "MCP server '%s': config value '%s' has hidden leading or "
            "trailing whitespace — this often causes authentication or "
            "connection failures. Check for stray spaces/newlines in "
            "config.yaml (or the referenced env var).",
            server_name,
            key_path,
        )
    return flagged


def _filter_suspicious_mcp_servers(servers: Dict[str, dict]) -> Dict[str, dict]:
    """Drop exfiltration-shaped MCP configs before any stdio spawn path."""
    try:
        from hermes_cli.mcp_security import validate_mcp_server_entry as _validate_mcp_server_entry
    except Exception:
        _validate_mcp_server_entry: Callable[[str, dict[str, Any]], list[str]] | None = None

    if _validate_mcp_server_entry is None:
        return servers

    safe_servers = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            safe_servers[name] = cfg
            continue
        issues = _validate_mcp_server_entry(name, cfg)
        if issues:
            logger.warning(
                "Skipping suspicious MCP server '%s': %s",
                name,
                "; ".join(issues),
            )
            continue
        safe_servers[name] = cfg
    return safe_servers


def _load_mcp_config() -> Dict[str, dict]:
    """Read ``mcp_servers`` from the Hermes config file.

    Returns a dict of ``{server_name: server_config}`` or empty dict.
    Server config can contain either ``command``/``args``/``env`` for stdio
    transport or ``url``/``headers`` for HTTP transport, plus optional
    ``timeout``, ``connect_timeout``, and ``auth`` overrides.

    ``${ENV_VAR}`` placeholders in string values are resolved from
    ``os.environ`` (which includes ``~/.hermes/.env`` loaded at startup).
    """
    try:
        from hermes_cli.config import load_config
        from utils import env_var_enabled as _env_enabled

        if _env_enabled("HERMES_SAFE_MODE"):
            return {}
        config = load_config()
        servers = config.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = {}
        # Ensure .env vars are available for interpolation
        try:
            from hermes_cli.env_loader import load_hermes_dotenv
            load_hermes_dotenv()
        except Exception:
            pass
        safe_servers: Dict[str, dict] = {}
        for name, cfg in _filter_suspicious_mcp_servers(servers).items():
            interpolated = _interpolate_env_vars(cfg)
            if isinstance(interpolated, dict):
                _warn_hidden_whitespace(name, interpolated)
                safe_servers[name] = interpolated
        try:
            from hermes_cli.plugins import discover_plugins, get_plugin_manager

            discover_plugins()
            portable = get_plugin_manager().get_portable_mcp_servers()
            for name, cfg in _filter_suspicious_mcp_servers(portable).items():
                if name in safe_servers:
                    logger.warning(
                        "Portable MCP server '%s' conflicts with native config; skipping",
                        name,
                    )
                    continue
                safe_servers[name] = dict(cfg)
        except Exception:
            logger.debug("Failed to load portable MCP servers", exc_info=True)
        return safe_servers
    except Exception as exc:
        logger.debug("Failed to load MCP config: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Server connection helper
# ---------------------------------------------------------------------------

async def _connect_server(name: str, config: dict) -> MCPServerTask:
    """Create an MCPServerTask, start it, and return when ready.

    The server Task keeps the connection alive in the background.
    Call ``server.shutdown()`` (on the same event loop) to tear it down.

    Raises:
        ValueError: if required config keys are missing.
        ImportError: if HTTP transport is needed but not available.
        Exception: on connection or initialization failure.
    """
    server = MCPServerTask(name)
    claim = _connect_server_claim.get()
    claim_token = None
    if claim is not None:
        claim(server)
        # ``start()`` creates the long-lived run task by copying this context.
        # The ownership callback is only for this connection attempt; do not
        # retain its discovery closure for the server's lifetime.
        claim_token = _connect_server_claim.set(None)
    try:
        await server.start(config)
    except asyncio.CancelledError:
        # start() already cancels/reaps server._task on external cancellation
        # (see the comment there) -- awaiting a redundant shutdown() inside a
        # cancelled context would only risk swallowing the cancellation.
        raise
    except BaseException:
        # Discovery owns claimed tasks and decides whether a failed start is a
        # live recoverable park or a terminal failure. Standalone probes have
        # no revival owner, so they must reap their failed task locally.
        if claim is None:
            try:
                await server.shutdown()
            except Exception as shutdown_exc:  # noqa: BLE001 -- best-effort reap, don't mask the real error
                logger.debug(
                    "MCP server '%s' shutdown during orphan-reap failed: %s",
                    name, shutdown_exc,
                )
        raise
    finally:
        if claim_token is not None:
            _connect_server_claim.reset(claim_token)
    return server


# ---------------------------------------------------------------------------
# Handler / check-fn factories
# ---------------------------------------------------------------------------

def _request_lazy_reconnect(server_name: str, server: MCPServerTask) -> bool:
    """Wake a recycled stdio server and wait briefly for a fresh session."""
    if not server._is_recycled_stdio():
        return False

    with _lock:
        loop = _mcp_loop
    if loop is None or not loop.is_running():
        return False

    def _signal_reconnect() -> None:
        server._ready.clear()
        server._reconnect_event.set()

    loop.call_soon_threadsafe(_signal_reconnect)

    async def _await_ready() -> bool:
        deadline = time.monotonic() + _RECYCLED_RECONNECT_TIMEOUT
        while time.monotonic() < deadline:
            if server.session is not None and server._ready.is_set():
                return True
            await asyncio.sleep(0.05)
        return False

    try:
        return bool(_run_on_mcp_loop(_await_ready, timeout=_RECYCLED_RECONNECT_TIMEOUT))
    except Exception as exc:
        logger.warning(
            "MCP server '%s': lazy reconnect after stdio recycle failed: %s",
            server_name, exc,
        )
        return False


def _resolve_server_lazy(name: str, config: dict) -> bool:
    """True when this server defers spawn/connect until first tool use.

    Gated per-server by ``mcp_servers.<name>.lazy`` in config (default OFF),
    following the same per-server key pattern as ``idle_timeout_seconds``.
    Design from #56832 (Vansh5632).
    """
    return _parse_boolish(config.get("lazy", False), default=False)


def _ensure_lazy_server_connected(server_name: str) -> bool:
    """Connect a lazily-registered MCP server on demand (sync, blocks caller).

    Composes with the existing connect machinery: respects the per-server
    connect cooldown (#50394), the ``_server_connecting`` dedup set, and
    routes through ``_discover_and_register_server`` so parked/recycle/
    cooldown bookkeeping stays in one place. Returns True when a live
    session is available afterwards.
    """
    with _lock:
        server = _servers.get(server_name)
        if server is not None and server.session is not None:
            return True
        config = _lazy_server_configs.get(server_name)
        if not config:
            return False
        if _connect_cooldown_active(server_name):
            return False
        if server_name in _server_connecting:
            return False
        _server_connecting.add(server_name)
        _server_connect_errors.pop(server_name, None)

    logger.info("MCP server '%s': lazy start on first use", server_name)
    _ensure_mcp_loop()
    connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)

    async def _connect():
        return await _discover_and_register_server(server_name, config)

    try:
        _run_on_mcp_loop(_connect, timeout=float(connect_timeout) + 30.0)
    except BaseException as exc:
        message = _format_connect_error(exc)
        with _lock:
            _server_connecting.discard(server_name)
            _server_connect_errors[server_name] = message
            _record_connect_failure(server_name)
        logger.warning(
            "Lazy MCP connect failed for '%s': %s", server_name, message,
        )
        return False

    with _lock:
        _server_connecting.discard(server_name)
        _clear_connect_failure(server_name)
        _lazy_server_configs.pop(server_name, None)
        stale_fingerprint = _lazy_server_fingerprints.pop(server_name, None)
        cached_names = _lazy_server_tool_names.pop(server_name, None) or []
        server = _servers.get(server_name)
        live_names = set(
            getattr(server, "_registered_tool_names", []) or []
        )
    # Stale-cache reconciliation: the cached manifest may advertise tools
    # the live server no longer serves. Deregister those phantoms so the
    # model stops seeing tools that can never succeed.
    phantom_names = [n for n in cached_names if n not in live_names]
    if phantom_names:
        from tools.registry import registry

        for tool_name in phantom_names:
            registry.deregister(tool_name)
            _forget_mcp_tool_server(tool_name)
        logger.info(
            "MCP server '%s': deregistered %d phantom cached tool(s) not "
            "served live (stale schema-cache fingerprint %s): %s",
            server_name, len(phantom_names), stale_fingerprint,
            ", ".join(phantom_names),
        )
    return server is not None and server.session is not None


def _get_connected_server_for_call(server_name: str) -> Optional[MCPServerTask]:
    """Return a connected server, lazily reconnecting recycled stdio state.

    Also the single first-use connect point for lazy (schema-cache
    registered) servers, so raw tool calls AND the resource/prompt utility
    handlers all trigger the deferred spawn (#56832).
    """
    with _lock:
        server = _servers.get(server_name)
        is_lazy = server_name in _lazy_server_configs
    if is_lazy and (server is None or server.session is None):
        _ensure_lazy_server_connected(server_name)
        with _lock:
            server = _servers.get(server_name)
        return server
    if server is not None and server.session is None and server._is_recycled_stdio():
        _request_lazy_reconnect(server_name, server)
        with _lock:
            server = _servers.get(server_name)
    return server


def _mark_server_call_started(server: Any) -> None:
    """Record a user-visible MCP operation when the server supports it."""
    mark_tool_call = getattr(server, "mark_tool_call", None)
    if callable(mark_tool_call):
        mark_tool_call()


@asynccontextmanager
async def _track_inflight_rpc(server: Any, server_name: str, op: str):
    """Register the running RPC on the server so teardown can fail it fast.

    Every user-visible request family wraps its RPC in this context
    (#48069 salvage). If a deliberate reconnect/shutdown teardown cancels
    the task (``_fail_inflight_calls`` sets ``_reconnecting`` first), the
    cancel is converted into a clean retryable RuntimeError instead of a raw
    CancelledError; external cancels (caller timeout, user interrupt)
    propagate unchanged.
    """
    inflight = getattr(server, "_inflight_tasks", None)
    task = asyncio.current_task()
    if task is not None and inflight is not None:
        # Test doubles may pass a bare SimpleNamespace; tracking is then
        # simply skipped (fast-fail teardown is a production-connection
        # feature, not something a fake needs).
        inflight.add(task)
    try:
        yield
    except asyncio.CancelledError:
        if getattr(server, "_reconnecting", False):
            raise RuntimeError(
                f"MCP {op} on '{server_name}' was aborted by a reconnect "
                f"teardown; retry the request on the rebuilt session"
            ) from None
        raise
    finally:
        if task is not None and inflight is not None:
            inflight.discard(task)


def _ensure_healthy_or_recycle(server: Any, server_name: str) -> None:
    """Health-check a suspect connection before its next call (#85125 3b).

    Implements the SuspectableBackend cheap-mark/lazy-verify contract at the
    dispatch boundary: a connection latched as suspect by a race or an auth
    error is probed once; a failed probe recycles it so the call below hits
    the normal reconnect path. A HEALTHY connection is never recycled here.
    """
    if not getattr(server, "_suspect_reason", None):
        return
    with _lock:
        loop = _mcp_loop
    if loop is None or not loop.is_running():
        return  # no background loop — nothing to verify against
    try:
        healthy = bool(_run_on_mcp_loop(server.ensure_healthy, timeout=15.0))
    except Exception as exc:  # never let the probe break dispatch
        logger.debug(
            "MCP server '%s': suspect health check errored: %s",
            server_name, exc,
        )
        healthy = False
    if not healthy:
        _signal_reconnect(server)


def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float):
    """Return a sync handler that calls an MCP tool via the background loop.

    The handler conforms to the registry's dispatch interface:
    ``handler(args_dict, **kwargs) -> str``
    """

    def _handler(args: dict, **kwargs) -> str:
        # Trust-tier gate (security boundary): write-capable tools on
        # servers configured ``trust: untrusted`` must be approved by the
        # user before ANY transport work happens — including the lazy
        # first-use spawn below. A denied call never touches the server.
        gate_error = _trust_gate_check(server_name, tool_name)
        if gate_error is not None:
            return gate_error

        # Circuit breaker: if this server has failed too many times
        # consecutively, short-circuit with a clear message so the model
        # stops retrying and uses alternative approaches (#10447).
        #
        # Once the cooldown elapses, the breaker transitions to
        # half-open: we let the *next* call through as a probe. On
        # success the success-path below resets the breaker; on
        # failure the error paths below bump the count again, which
        # re-stamps the open-time via _bump_server_error (re-arming
        # the cooldown).
        if _server_error_counts.get(server_name, 0) >= _CIRCUIT_BREAKER_THRESHOLD:
            opened_at = _server_breaker_opened_at.get(server_name, 0.0)
            age = time.monotonic() - opened_at
            if age < _CIRCUIT_BREAKER_COOLDOWN_SEC:
                remaining = max(1, int(_CIRCUIT_BREAKER_COOLDOWN_SEC - age))
                return tool_error(
                    f"MCP server '{server_name}' is unreachable after "
                    f"{_server_error_counts[server_name]} consecutive "
                    f"failures. Auto-retry available in ~{remaining}s. "
                    f"Do NOT retry this tool yet — use alternative "
                    f"approaches or ask the user to check the MCP server."
                )
            # Cooldown elapsed → fall through as a half-open probe.

        server = _get_connected_server_for_call(server_name)
        if not server:
            _bump_server_error(server_name)
            return tool_error(f"MCP server '{server_name}' is not connected")

        if not server.session:
            # No live session. A reconnect may already be completing (the
            # transport swaps in a fresh session object asynchronously) —
            # wait briefly before treating this as a failure, so a
            # transient reconnect window doesn't burn a circuit-breaker
            # strike (#26892).
            if _wait_for_server_session_ready(
                server, timeout=min(5.0, float(tool_timeout or 5.0)),
            ):
                pass  # Fresh session arrived; proceed below.
            else:
                # Still down — the server task is reconnecting, or it has
                # exhausted its retry budget and parked (e.g. a dead stdio
                # subprocess). Probing here would write into a dead/absent
                # transport and re-arm the breaker forever (#16788). Instead,
                # ask the (always-present) server task to rebuild the
                # transport — which respawns a dead stdio subprocess — and
                # return a clean "reconnecting" error so the model backs off
                # without burning iterations. The breaker resets once the
                # fresh session initializes (_run_stdio/_run_http call
                # _reset_server_error).
                _bump_server_error(server_name)
                if _signal_reconnect(server):
                    return tool_error(
                        f"MCP server '{server_name}' transport is down; "
                        f"reconnect requested. Do NOT retry this tool "
                        f"immediately — give it a few seconds to come back."
                    )
                return tool_error(f"MCP server '{server_name}' is not connected")

        async def _call():
            _mark_server_call_started(server)
            async with server._rpc_lock, _track_inflight_rpc(
                server, server_name, f"tools/call {tool_name}"
            ):
                # Snapshot the agent's context so an elicitation callback
                # triggered during this call (fired on the MCP recv loop
                # task, which doesn't inherit our contextvars) can replay
                # it and detect the gateway platform / session for routing.
                server._pending_call_context = contextvars.copy_context()
                try:
                    # Fast-fail (#81995): a stdio subprocess that is already
                    # dead must not own this call slot — fail immediately
                    # instead of waiting out the full tool timeout on a
                    # transport nobody will ever answer.
                    _stdio_dead = getattr(server, "_stdio_children_dead", None)
                    # callable() + real-bool result: MagicMock attributes return
                    # truthy Mocks, which would spuriously trip the fast-fail.
                    if (
                        callable(_stdio_dead)
                        and isinstance(_stdio_dead_result := _stdio_dead(), bool)
                        and _stdio_dead_result
                    ):
                        # Dead children but stale server.session, so the
                        # transport-down path above never fired — signal the
                        # server task to respawn and return a clean
                        # reconnecting error. No explicit _bump_server_error:
                        # the error return flows through the handler's JSON
                        # parse, which already bumps once.
                        if _signal_reconnect(server):
                            return tool_error(
                                f"MCP server '{server_name}' stdio subprocess is "
                                f"dead and reconnect was requested. Do NOT retry "
                                f"immediately — give it a few seconds to respawn."
                            )
                        raise TimeoutError(
                            f"MCP stdio subprocess for '{server_name}' has "
                            f"exited; failing the call fast instead of "
                            f"waiting {float(tool_timeout):.0f}s"
                        )
                    _call_coro = server.session.call_tool(tool_name, arguments=args)
                    _watch_children = getattr(server, "_watch_stdio_children", None)
                    _watch_ok = (
                        _watch_children is not None
                        and inspect.isawaitable(_watch_children())
                        and asyncio.iscoroutine(_call_coro)
                    )
                    if not _watch_ok:
                        # Stubbed sessions (MagicMock in tests) return a
                        # non-awaitable, or there is no child-watcher to race
                        # against: plain await is exactly the pre-#81995
                        # semantics.
                        result = (
                            await _call_coro
                            if asyncio.iscoroutine(_call_coro)
                            else _call_coro
                        )
                    else:
                        # Fast-fail machinery (#81995): the RPC races a
                        # stdio-children watcher so a dead subprocess fails
                        # the call immediately instead of riding out the full
                        # tool timeout.
                        rpc_task = asyncio.ensure_future(_call_coro)
                        watch_task = asyncio.ensure_future(_watch_children())
                        try:
                            done, _pending = await asyncio.wait(
                                {rpc_task, watch_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if watch_task in done and not rpc_task.done():
                                rpc_task.cancel()
                                # Same stale-session problem as the pre-call
                                # gate above: the subprocess died mid-call but
                                # nothing clears server.session, so without a
                                # reconnect signal the server would stay dead
                                # until the idle keepalive probe notices.
                                _signal_reconnect(server)
                                raise TimeoutError(
                                    f"MCP stdio subprocess for '{server_name}' "
                                    f"exited mid-call; failing the call fast "
                                    f"instead of waiting "
                                    f"{float(tool_timeout):.0f}s; reconnect "
                                    f"requested — give it a few seconds to "
                                    f"respawn before retrying"
                                )
                            result = await rpc_task
                        finally:
                            watch_task.cancel()
                            if not rpc_task.done():
                                rpc_task.cancel()
                            await asyncio.gather(
                                rpc_task, watch_task, return_exceptions=True
                            )
                finally:
                    server._pending_call_context = None
            # The RPC round-trip completed — the session is demonstrably
            # healthy at the transport level (even if the tool itself
            # returned isError). Clear the rapid-drop budget (#62212).
            _mark_proven = getattr(server, "_mark_session_proven", None)
            if _mark_proven is not None:
                _mark_proven()
            # MCP CallToolResult has .content (list of content blocks) and
            # .is_error (.isError before mcp 2.0)
            if mcp_field(result, "is_error", "isError", False):
                error_text = ""
                for block in (result.content or []):
                    if getattr(block, "text", None):
                        error_text += block.text
                        continue
                    # EmbeddedResource blocks inside error payloads carry
                    # their text under .resource.text — previously dropped,
                    # leaving a bare "MCP tool returned an error".
                    res_text = getattr(getattr(block, "resource", None), "text", None)
                    if res_text:
                        error_text += str(res_text)
                return tool_error(_truncate_mcp_text_result(
                    _sanitize_error(
                        error_text or "MCP tool returned an error"
                    )
                ))

            # Collect text from content blocks. MCP tool results can also
            # include ImageContent blocks (screenshot / Blockbench / Playwright
            # etc.); cache those via the gateway's image-cache helper so they
            # flow through Hermes' MEDIA: tag convention and out to messaging
            # adapters that render images natively. Without this, image blocks
            # were silently dropped and the agent got an empty response.
            #
            # Distilled from #17915 (c3115644151) and #10848 (gnanirahulnutakki),
            # both too stale to cherry-pick. #10848's approach (integrate with
            # Hermes' MEDIA tag + cache_image_from_bytes) was the cleaner of
            # the two — plugs into existing infrastructure.
            parts: List[str] = []
            for block in (result.content or []):
                if hasattr(block, "text") and block.text:
                    # Primary CallToolResult content is ordinary model-facing
                    # prose.  Keep opaque identifiers unless one of the
                    # evidence-backed credential passes identifies them.
                    parts.append(_sanitize_mcp_text_leaf(
                        strip_unicode_tags(block.text), "primary_content"
                    ))
                    continue
                image_tag = _cache_mcp_image_block(block)
                if image_tag:
                    parts.append(image_tag)
                    continue
                audio_tag = _cache_mcp_audio_block(block)
                if audio_tag:
                    parts.append(audio_tag)
                    continue
                # ResourceLink / EmbeddedResource blocks (PDFs, archives,
                # office docs, ...). Previously these were silently dropped,
                # so document-oriented MCP tools appeared to return metadata
                # only (enterprise customer report, 2026-07).
                resource_text = _render_mcp_resource_block(block, server_name)
                if resource_text:
                    parts.append(resource_text)
                    continue
                # Benign empty renders (empty text blocks, empty text
                # resources, audio in a process without the gateway cache)
                # aren't data loss — log at debug. Warn only for genuinely
                # unrecognized block shapes.
                block_type = getattr(block, "type", None) or type(block).__name__
                if block_type in {"text", "resource", "audio", "image"}:
                    logger.debug(
                        "MCP %s: content block type %r rendered empty",
                        server_name, block_type,
                    )
                else:
                    logger.warning(
                        "MCP %s: dropping unsupported content block type %r",
                        server_name, block_type,
                    )
            text_result = _sanitize_mcp_result_value(
                "\n".join(parts) if parts else "",
                "primary_content",
            )

            # Hard-cap pathological payloads before they propagate (#56059);
            # ordinary large results pass untouched to the spillover layer.
            text_result = _truncate_mcp_text_result(text_result)

            # Combine content + structuredContent when both are present.
            # MCP spec: content is model-oriented (text), structuredContent
            # is machine-oriented (JSON metadata).  For an AI agent, content
            # is the primary payload; structuredContent supplements it.
            #
            # Server-level `_meta` is also surfaced (ported from
            # MoonshotAI/kimi-code#2596): servers return namespaced metadata
            # there (validated contracts, browser-handoff payloads, ...) that
            # was previously invisible to the agent. Protocol-reserved keys
            # are dropped first (kimi-code#2600) — per the MCP spec's key-name
            # rules a prefix is reserved when a `modelcontextprotocol` or
            # `mcp` label is followed by at least one more label (e.g.
            # `modelcontextprotocol.io/...`, `tools.mcp.com/...`); those carry
            # host/protocol plumbing, not model-facing data. Unprefixed and
            # vendor-namespaced keys (`com.example.mcp/...`) pass through —
            # their semantics belong to the server.
            structured = _sanitize_mcp_result_value(
                mcp_field(result, "structured_content", "structuredContent")
            )
            # Cap structuredContent too — a malicious server could flood
            # context via a multi-MB JSON payload (#56059). When the
            # serialized form exceeds the hard cap, replace it with the
            # truncated string (head + tail preserved) so it degrades
            # gracefully instead of flooding downstream.
            if structured is not None:
                try:
                    _structured_json = json.dumps(structured, ensure_ascii=False)
                except (TypeError, ValueError):
                    _structured_json = None
                if _structured_json is not None and len(_structured_json) > _MCP_HARD_RESULT_CAP_CHARS:
                    structured = _truncate_mcp_text_result(_structured_json)
            meta = _sanitize_mcp_result_value(
                _strip_reserved_meta_keys(mcp_field(result, "meta", "meta"))
            )
            if structured is not None or meta is not None:
                payload: Dict[str, Any] = {}
                if text_result:
                    payload["result"] = text_result
                if structured is not None:
                    if text_result:
                        payload["structuredContent"] = structured
                    else:
                        payload["result"] = structured
                if meta:
                    payload["_meta"] = meta
                if "result" not in payload:
                    payload["result"] = text_result
                try:
                    return json.dumps(payload, ensure_ascii=False)
                except (TypeError, ValueError):
                    # Non-serializable metadata: drop the extras rather than
                    # failing the whole tool call.
                    return json.dumps({"result": text_result}, ensure_ascii=False)
            return json.dumps({"result": text_result}, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            result = _call_once()
            # Check if the MCP tool itself returned an error
            try:
                parsed = json.loads(result)
                if "error" in parsed:
                    _bump_server_error(server_name)
                else:
                    _reset_server_error(server_name)  # success — reset
            except (json.JSONDecodeError, TypeError):
                _reset_server_error(server_name)  # non-JSON = success
            return result
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            # Auth-specific recovery path: consult the manager, signal
            # reconnect if viable, retry once. Returns None to fall
            # through for non-auth exceptions.
            recovered = _handle_auth_error_and_retry(
                server_name, exc, _call_once,
                f"tools/call {tool_name}",
            )
            if recovered is not None:
                return recovered

            # Transport session expiry (#13383): same reconnect flow
            # but skips OAuth recovery because the access token is
            # still valid — only the server-side session is stale.
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once,
                f"tools/call {tool_name}",
            )
            if recovered is not None:
                return recovered

            _bump_server_error(server_name)
            logger.error(
                "MCP tool %s/%s call failed: %s",
                server_name, tool_name, exc,
            )
            return tool_error(_sanitize_error(
                f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
            ))

    return _handler


def _make_list_resources_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that lists resources from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _get_connected_server_for_call(server_name)
        if not server or not server.session:
            return tool_error(f"MCP server '{server_name}' is not connected")

        async def _call():
            _mark_server_call_started(server)
            async with server._rpc_lock:
                all_resources = await _paginate_full_list(
                    server.session.list_resources, "resources", server_name
                )
            resources = []
            for r in all_resources:
                entry = {}
                if hasattr(r, "uri"):
                    entry["uri"] = str(r.uri)
                if hasattr(r, "name"):
                    entry["name"] = r.name
                if hasattr(r, "description") and r.description:
                    entry["description"] = r.description
                # Key stays camelCase — this dict is the tool's own JSON
                # output shape, not an SDK model.
                _mime = mcp_field(r, "mime_type", "mimeType")
                if _mime:
                    entry["mimeType"] = _mime
                resources.append(entry)
            return json.dumps(
                _sanitize_mcp_result_value({"resources": resources}),
                ensure_ascii=False,
            )

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_auth_error_and_retry(
                server_name, exc, _call_once, "resources/list",
            )
            if recovered is not None:
                return recovered
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "resources/list",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/list_resources failed: %s", server_name, exc,
            )
            return tool_error(_sanitize_error(
                f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
            ))

    return _handler


def _make_read_resource_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that reads a resource by URI from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _get_connected_server_for_call(server_name)
        if not server or not server.session:
            return tool_error(f"MCP server '{server_name}' is not connected")

        uri = args.get("uri")
        if not uri:
            return tool_error("Missing required parameter 'uri'")

        async def _call():
            _mark_server_call_started(server)
            async with server._rpc_lock:
                result = await server.session.read_resource(uri)
            # read_resource returns ReadResourceResult with .contents list
            parts: List[str] = []
            contents = result.contents if hasattr(result, "contents") else []
            for block in contents:
                block_text = getattr(block, "text", None)
                if block_text is not None:
                    if isinstance(block_text, str):
                        parts.append(_sanitize_mcp_text_leaf(
                            strip_unicode_tags(block_text), "text"
                        ))
                    continue
                elif getattr(block, "blob", None) is not None:
                    # Materialize binary resource contents into the document
                    # cache instead of discarding them (same contract as
                    # EmbeddedResource blocks in tool results).
                    rendered = _render_mcp_resource_block(
                        SimpleNamespace(type="resource", resource=block),
                        server_name,
                    )
                    parts.append(rendered or f"[binary data, {len(block.blob)} bytes]")
            return json.dumps(
                _sanitize_mcp_result_value(
                    {"result": "\n".join(parts) if parts else ""}
                ),
                ensure_ascii=False,
            )

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_auth_error_and_retry(
                server_name, exc, _call_once, "resources/read",
            )
            if recovered is not None:
                return recovered
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "resources/read",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/read_resource failed: %s", server_name, exc,
            )
            return tool_error(_sanitize_error(
                f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
            ))

    return _handler


def _make_list_prompts_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that lists prompts from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _get_connected_server_for_call(server_name)
        if not server or not server.session:
            return tool_error(f"MCP server '{server_name}' is not connected")

        async def _call():
            _mark_server_call_started(server)
            async with server._rpc_lock:
                all_prompts = await _paginate_full_list(
                    server.session.list_prompts, "prompts", server_name
                )
            prompts = []
            for p in all_prompts:
                entry = {}
                if hasattr(p, "name"):
                    entry["name"] = p.name
                if hasattr(p, "description") and p.description:
                    entry["description"] = p.description
                if hasattr(p, "arguments") and p.arguments:
                    entry["arguments"] = [
                        {
                            "name": a.name,
                            **({"description": a.description} if hasattr(a, "description") and a.description else {}),
                            **({"required": a.required} if hasattr(a, "required") else {}),
                        }
                        for a in p.arguments
                    ]
                prompts.append(entry)
            return json.dumps(
                _sanitize_mcp_result_value({"prompts": prompts}),
                ensure_ascii=False,
            )

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_auth_error_and_retry(
                server_name, exc, _call_once, "prompts/list",
            )
            if recovered is not None:
                return recovered
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "prompts/list",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/list_prompts failed: %s", server_name, exc,
            )
            return tool_error(_sanitize_error(
                f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
            ))

    return _handler


def _make_get_prompt_handler(server_name: str, tool_timeout: float):
    """Return a sync handler that gets a prompt by name from an MCP server."""

    def _handler(args: dict, **kwargs) -> str:
        server = _get_connected_server_for_call(server_name)
        if not server or not server.session:
            return tool_error(f"MCP server '{server_name}' is not connected")

        name = args.get("name")
        if not name:
            return tool_error("Missing required parameter 'name'")
        arguments = args.get("arguments", {})

        async def _call():
            _mark_server_call_started(server)
            async with server._rpc_lock:
                result = await server.session.get_prompt(name, arguments=arguments)
            # GetPromptResult has .messages list
            messages = []
            for msg in (result.messages if hasattr(result, "messages") else []):
                entry = {}
                if hasattr(msg, "role"):
                    entry["role"] = msg.role
                if hasattr(msg, "content"):
                    content = msg.content
                    if hasattr(content, "text"):
                        content_text = content.text
                        entry["content"] = (
                            strip_unicode_tags(content_text)
                            if isinstance(content_text, str)
                            else content_text
                        )
                    elif isinstance(content, str):
                        entry["content"] = strip_unicode_tags(content)
                    else:
                        entry["content"] = content
                messages.append(entry)
            resp = {"messages": messages}
            if hasattr(result, "description") and result.description:
                resp["description"] = result.description
            return json.dumps(
                _sanitize_mcp_result_value(resp),
                ensure_ascii=False,
            )

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_auth_error_and_retry(
                server_name, exc, _call_once, "prompts/get",
            )
            if recovered is not None:
                return recovered
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "prompts/get",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/get_prompt failed: %s", server_name, exc,
            )
            return tool_error(_sanitize_error(
                f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
            ))

    return _handler
'''

EXPORTED_NAMES = ('_get_auth_error_types', '_is_auth_error', '_handle_auth_error_and_retry', '_SESSION_EXPIRED_MARKERS', '_EXC_TRAVERSAL_MAX_NODES', '_is_session_expired_error', '_handle_session_expired_and_retry', '_parallel_safe_servers', '_mcp_tool_server_names', '_mcp_loop', '_mcp_thread', '_lock', '_LOCK_UNAVAILABLE', '_MCP_DISCOVERY_LOCK_PATH', '_MCP_DISCOVERY_LOCK_MAX_RETRIES', '_MCP_DISCOVERY_LOCK_RETRY_DELAY_S', '_LockCookie', '_acquire_lock_on_fh', '_try_acquire_mcp_discovery_lock', '_stdio_pids', '_orphan_stdio_pids', '_orphan_stdio_pid_servers', '_stdio_pgids', '_snapshot_child_pids', '_NON_MCP_CHILD_CMDLINE_MARKERS', '_filter_mcp_children', '_mcp_loop_exception_handler', '_ensure_mcp_loop', '_wrap_with_home_override', '_wrap_with_dashboard_oauth_flow', '_run_on_mcp_loop', '_interrupted_call_result', '_interpolate_env_vars', '_whitespace_warned', '_warn_hidden_whitespace', '_filter_suspicious_mcp_servers', '_load_mcp_config', '_connect_server', '_request_lazy_reconnect', '_resolve_server_lazy', '_ensure_lazy_server_connected', '_get_connected_server_for_call', '_mark_server_call_started', '_track_inflight_rpc', '_ensure_healthy_or_recycle', '_make_tool_handler', '_make_list_resources_handler', '_make_read_resource_handler', '_make_list_prompts_handler', '_make_get_prompt_handler')
SOURCE_PATH = Path(__file__)

def install(namespace: dict[str, object]) -> None:
    filename = str(SOURCE_PATH)
    linecache.cache[filename] = (
        len(_SOURCE), None, _SOURCE.splitlines(True), filename
    )
    exec(compile(_SOURCE, filename, "exec"), namespace, namespace)
