"""Transport abstraction for the tui_gateway JSON-RPC server.

Historically the gateway wrote every JSON frame directly to real stdout.  This
module decouples the I/O sink from the handler logic so the same dispatcher
can be driven over stdio (``tui_gateway.entry``) or WebSocket
(``tui_gateway.ws``) without duplicating code.

A :class:`Transport` is anything that can accept a JSON-serialisable dict and
forward it to its peer.  The active transport for the current request is
tracked in a :class:`contextvars.ContextVar` so handlers — including those
dispatched onto the worker pool — route their writes to the right peer.

Backward compatibility
----------------------
``tui_gateway.server.write_json`` still works without any transport bound.
When nothing is on the contextvar and no session-level transport is found,
it falls back to the module-level :class:`StdioTransport`, which wraps the
original ``_real_stdout`` + ``_stdout_lock`` pair.  Tests that monkey-patch
``server._real_stdout`` continue to work because the stdio transport resolves
the stream lazily through a callback.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import errno
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Optional, Protocol, runtime_checkable

# Errno values that mean "the peer is gone" rather than "the host has a
# real I/O problem".  Anything outside this set re-raises so it surfaces
# in the crash log instead of looking like a clean disconnect.
_PEER_GONE_ERRNOS = frozenset({
    errno.EPIPE,        # write to closed pipe (POSIX)
    errno.ECONNRESET,   # peer reset the connection
    errno.EBADF,        # fd closed under us
    errno.ESHUTDOWN,    # transport endpoint shut down
    getattr(errno, "WSAECONNRESET", -1),  # win32 mapping (no-op on POSIX)
    getattr(errno, "WSAESHUTDOWN", -1),
} - {-1})

logger = logging.getLogger(__name__)

# Optional knob: when true, StdioTransport does not call ``stream.flush``
# after writing.  Use this on environments where a half-closed pipe (TUI
# Node parent quit while the gateway is still emitting events) makes
# flush block long enough to starve the rest of the worker pool.
#
# IMPORTANT: Python text stdout is fully buffered when attached to a
# pipe (the TUI case), so this knob ONLY makes sense when the gateway
# is launched with ``-u`` or ``PYTHONUNBUFFERED=1``.  Without one of
# those, JSON-RPC frames will accumulate in the buffer and the TUI
# will hang waiting for ``gateway.ready``.  Default stays off so the
# existing flush-after-write behaviour is unchanged.
_DISABLE_FLUSH = (os.environ.get("HERMES_TUI_GATEWAY_NO_FLUSH", "") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Worker pool behind FanoutTransport.write.  A non-streaming WebSocket write
# blocks the calling thread while the owning event loop flushes the frame — up
# to ``tui_gateway.ws._WS_WRITE_TIMEOUT_S`` (10s) when that loop is stalled —
# so walking a session's peers one at a time lets one wedged client hold the
# frame back from every healthy client behind it.  The pool is created lazily
# and only ever used when a session has more than one peer, so the ordinary
# single-client gateway never starts a thread.
#
# Sizing: these workers are idle except while a peer is actually wedged, so the
# cap only has to cover the wedged peers of all sessions at once.  If it were
# ever saturated the excess writes queue and run in submission order, which is
# the pre-pool behaviour — degraded, not broken.
_FANOUT_POOL_MAX_WORKERS = 32

# Wall-clock bound on ONE fan-out write, measured from before the first peer is
# dispatched.  Deliberately larger than ``tui_gateway.ws._WS_WRITE_TIMEOUT_S``
# (10.0) so a merely-slow peer reaches its own timeout and reports for itself;
# this deadline only exists so a peer that never returns at all cannot pin the
# emitting thread forever.  Not imported from ``tui_gateway.ws``: that module
# imports ``tui_gateway.server``, which imports this one, so the import would
# be circular.  Drift is harmless — a peer that misses this deadline is treated
# as in-flight, not as dead (see ``FanoutTransport.write``).
_FANOUT_WRITE_DEADLINE_S = 12.0

_fanout_pool: "concurrent.futures.ThreadPoolExecutor | None" = None
_fanout_pool_lock = threading.Lock()


def _fanout_write_pool() -> "concurrent.futures.ThreadPoolExecutor":
    """The shared fan-out pool, created on first multi-peer write."""
    global _fanout_pool
    pool = _fanout_pool
    if pool is not None:
        return pool
    with _fanout_pool_lock:
        if _fanout_pool is None:
            _fanout_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=_FANOUT_POOL_MAX_WORKERS,
                thread_name_prefix="tui-fanout",
            )
        return _fanout_pool


def _caller_is_on_event_loop() -> bool:
    """True when the CALLING thread is running an asyncio event loop.

    Mirrors the probe in ``tui_gateway.ws.WSTransport.write``, which takes a
    fire-and-forget path when it can see it is on its own loop and a BLOCKING
    ``fut.result`` path when it cannot.  ``FanoutTransport`` owns no loop and
    its peers may sit on different ones, so the test here is the conservative
    one: if this thread is running any loop at all, keep the peer writes on it.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@runtime_checkable
class Transport(Protocol):
    """Minimal interface every transport implements."""

    def write(self, obj: dict) -> bool:
        """Emit one JSON frame. Return ``False`` when the peer is gone."""

    def close(self) -> None:
        """Release any resources owned by this transport."""


_current_transport: contextvars.ContextVar[Optional[Transport]] = (
    contextvars.ContextVar(
        "hermes_gateway_transport",
        default=None,
    )
)


def current_transport() -> Optional[Transport]:
    """Return the transport bound for the current request, if any."""
    return _current_transport.get()


def bind_transport(transport: Optional[Transport]):
    """Bind *transport* for the current context. Returns a token for :func:`reset_transport`."""
    return _current_transport.set(transport)


def reset_transport(token) -> None:
    """Restore the transport binding captured by :func:`bind_transport`."""
    _current_transport.reset(token)


class StdioTransport:
    """Writes JSON frames to a stream (usually ``sys.stdout``).

    The stream is resolved via a callable so runtime monkey-patches of the
    underlying stream continue to work — this preserves the behaviour the
    existing test suite relies on (``monkeypatch.setattr(server, "_real_stdout", ...)``).
    """

    __slots__ = ("_stream_getter", "_lock")

    def __init__(self, stream_getter: Callable[[], Any], lock: threading.Lock) -> None:
        self._stream_getter = stream_getter
        self._lock = lock

    def write(self, obj: dict) -> bool:
        """Return ``True`` on success, ``False`` ONLY when the peer is gone.

        Returning ``False`` is the dispatcher's "broken stdout pipe" signal
        — ``entry.py`` calls ``sys.exit(0)`` when ``write_json`` reports
        ``False``.  So programming errors (non-JSON-safe payloads, encoding
        misconfig, unexpected ValueErrors, host I/O bugs like ENOSPC) MUST
        NOT return ``False``, otherwise a real bug looks like a clean
        disconnect and is harder to diagnose.  Those re-raise so the
        existing crash-log infrastructure records the traceback.

        Peer-gone branches:
          * ``BrokenPipeError``
          * ``ValueError("...closed file...")``
          * ``OSError`` whose errno is in :data:`_PEER_GONE_ERRNOS`
            (EPIPE / ECONNRESET / EBADF / ESHUTDOWN; plus WSA mappings
            on Windows).  Other OSError errnos (ENOSPC, EACCES, ...) are
            real host problems and re-raise.
        """
        # Serialization is OUTSIDE the lock so a large payload can't
        # block other threads emitting their own frames.  A non-JSON-safe
        # payload is a programming error: re-raise so the crash log
        # captures it instead of silently exiting via the False path.
        line = json.dumps(obj, ensure_ascii=False) + "\n"

        with self._lock:
            stream = self._stream_getter()
            try:
                stream.write(line)
            except BrokenPipeError:
                return False
            except ValueError as e:
                # ValueError("I/O operation on closed file") is the
                # ONLY ValueError that means "peer gone".  Anything
                # else — including UnicodeEncodeError, which is a
                # ValueError subclass for misconfigured locales —
                # is a real bug; re-raise so it surfaces in the crash log.
                if isinstance(e, UnicodeEncodeError) or "closed file" not in str(e):
                    raise
                return False
            except OSError as e:
                if e.errno not in _PEER_GONE_ERRNOS:
                    raise
                logger.debug("StdioTransport write peer gone: %s", e)
                return False

            # A flush that *raises* with a peer-gone errno means the
            # dispatcher should exit cleanly.  A flush that *hangs* on
            # a half-closed pipe holds the lock until it returns — see
            # ``_DISABLE_FLUSH`` for the "skip flush entirely" escape
            # hatch.
            if not _DISABLE_FLUSH:
                try:
                    stream.flush()
                except BrokenPipeError:
                    return False
                except ValueError as e:
                    if isinstance(e, UnicodeEncodeError) or "closed file" not in str(e):
                        raise
                    return False
                except OSError as e:
                    if e.errno not in _PEER_GONE_ERRNOS:
                        raise
                    logger.debug("StdioTransport flush peer gone: %s", e)
                    return False

        return True

    def close(self) -> None:
        return None


class FanoutTransport:
    """Delivers one JSON frame to every client attached to a session.

    A session's ``transport`` slot used to hold exactly one client, so every
    ``prompt.submit`` / ``session.resume`` / ``session.activate`` / queued-prompt
    drain REBOUND it and whichever client was there before went silent.  This
    object goes in the same slot and satisfies the same :class:`Transport`
    protocol, so ``server.write_json`` — and every other reader of that slot —
    is unchanged; the only difference is that N clients receive the frame
    instead of the most recent one.

    Scope: this carries a session's ASYNC EVENT stream only.  Request/response
    RPCs still answer on the request's context-bound transport
    (``dispatch`` → ``current_transport()``), so a client only ever sees replies
    to its own calls.

    Peers are pruned on failure: a transport that returns ``False`` (peer gone)
    or raises is dropped from the fan-out instead of being written to forever.
    Pruning is about dead peers, not slow ones — a client that is merely wedged
    is kept, and :meth:`write` runs the peer writes concurrently so its stall
    stays its own.
    """

    __slots__ = ("_lock", "_transports")

    def __init__(self, *transports: "Transport") -> None:
        self._lock = threading.Lock()
        self._transports: list["Transport"] = []
        for transport in transports:
            self.attach(transport)

    def attach(self, transport: "Transport") -> bool:
        """Add *transport*. Returns ``True`` when it was not already attached."""
        if transport is None or transport is self:
            return False
        with self._lock:
            for existing in self._transports:
                if existing is transport:
                    return False
            self._transports.append(transport)
            return True

    def detach(self, transport: "Transport") -> bool:
        """Remove *transport*. Returns ``True`` when it was attached."""
        with self._lock:
            for idx, existing in enumerate(self._transports):
                if existing is transport:
                    del self._transports[idx]
                    return True
        return False

    def contains(self, transport: "Transport") -> bool:
        with self._lock:
            return any(existing is transport for existing in self._transports)

    def transports(self) -> list["Transport"]:
        """A snapshot of the attached transports (safe to iterate)."""
        with self._lock:
            return list(self._transports)

    def has_transports(self, *, excluding: "Transport | None" = None) -> bool:
        """True when any transport other than *excluding* is still attached."""
        with self._lock:
            return any(existing is not excluding for existing in self._transports)

    @staticmethod
    def _deliver(transport: "Transport", obj: dict, dead: list) -> bool:
        """Write *obj* to one peer inline.  Records a dead peer in *dead*.

        Returns ``True`` when the peer took the frame.
        """
        try:
            ok = transport.write(obj)
        except Exception:
            logger.debug("fanout write failed; pruning peer", exc_info=True)
            dead.append(transport)
            return False
        if ok:
            return True
        dead.append(transport)
        return False

    def write(self, obj: dict) -> bool:
        """Deliver *obj* to every attached transport.

        Iterates a SNAPSHOT and never holds the lock across a peer write, so a
        peer write can never block an attach or a detach.

        With more than one peer the writes run CONCURRENTLY: every peer but one
        is handed to a shared worker pool and the last runs on the calling
        thread, which is going to block here anyway.  Serial delivery would let
        a client wedged for the full WS write timeout hold the frame back from
        every healthy client behind it in the list; this way each peer's stall
        is its own.  Two cases stay inline:

        * exactly one peer, so a single-client session is untouched;
        * a caller already running on an event loop.  ``WSTransport.write``
          fires and forgets when it sees its own loop and BLOCKS on
          ``fut.result`` when it does not, so moving a loop-thread write onto a
          pool thread would make that thread wait on the loop it just left
          while the loop waits on it — a full write-timeout freeze.

        Pruning is unchanged: a peer that returns ``False`` or raises is
        detached, whichever path it took.  A peer that has not answered by
        ``_FANOUT_WRITE_DEADLINE_S`` is NOT pruned — slow is not dead — and
        counts as delivered, which is what ``WSTransport.write`` itself reports
        when its own write times out: the frame is queued on that peer's loop
        and flushes when the loop breathes.  If that peer really is gone, its
        next write returns ``False`` promptly and prunes it then.

        The collect happens before the return, so frame A is on every peer that
        answered within the deadline before frame B is dispatched to any of
        them.  A peer whose write was still queued on the pool when the deadline
        passed is the exception: the next frame can reach that peer's own
        ``_token_lock`` while the previous one is still waiting for it, and the
        lock — not this method — decides the order they land in.  Within a peer,
        ordering is that peer's own business (``WSTransport`` queues under its
        token lock, ``StdioTransport`` writes under its stream lock);
        ``server.write_json`` takes no lock, so concurrent emitters interleave
        here exactly as they already did.

        Returns ``True`` when at least one client accepted the frame or still
        has it in flight — an all-dead fan-out reports peer-gone exactly like a
        single dead transport would.
        """
        targets = self.transports()
        if not targets:
            return False

        dead: list["Transport"] = []
        delivered = False

        if len(targets) == 1 or _caller_is_on_event_loop():
            for transport in targets:
                if self._deliver(transport, obj, dead):
                    delivered = True
            for transport in dead:
                self.detach(transport)
            return delivered

        deadline = time.monotonic() + _FANOUT_WRITE_DEADLINE_S
        pool = _fanout_write_pool()
        dispatched: list[tuple["Transport", "concurrent.futures.Future | None"]] = []
        for transport in targets[:-1]:
            try:
                dispatched.append((transport, pool.submit(transport.write, obj)))
            except RuntimeError:
                # Pool refused the work (interpreter shutting down).  Deliver
                # inline below rather than dropping the frame.
                dispatched.append((transport, None))

        # The last peer runs here: one fewer worker per write, and a two-client
        # session costs a single pool thread.
        if self._deliver(targets[-1], obj, dead):
            delivered = True

        # One deadline for the whole batch, then read only the futures that
        # finished.  Waiting first and calling ``result()`` on a settled future
        # keeps our deadline distinguishable from a peer that raised: on 3.11+
        # ``concurrent.futures.TimeoutError`` IS the builtin ``TimeoutError``,
        # so a ``result(timeout=...)`` cannot tell the two apart.
        futures = [fut for _, fut in dispatched if fut is not None]
        if futures:
            concurrent.futures.wait(
                futures, timeout=max(0.0, deadline - time.monotonic())
            )

        for transport, fut in dispatched:
            if fut is None:
                if self._deliver(transport, obj, dead):
                    delivered = True
                continue
            if not fut.done():
                # Slow, not dead: leave it attached and count the frame as in
                # flight.  Cancelling would not stop a write already running.
                logger.warning(
                    "fanout write still pending after %ss; peer left attached",
                    _FANOUT_WRITE_DEADLINE_S,
                )
                delivered = True
                continue
            try:
                ok = fut.result()
            except Exception:
                logger.debug("fanout write failed; pruning peer", exc_info=True)
                dead.append(transport)
                continue
            if ok:
                delivered = True
            else:
                dead.append(transport)

        for transport in dead:
            self.detach(transport)
        return delivered

    def close(self) -> None:
        """Detach every peer. Does NOT close them — each owns its own socket."""
        with self._lock:
            self._transports = []


class TeeTransport:
    """Mirrors writes to one primary plus N best-effort secondaries.

    The primary's return value (and exceptions) determine the result —
    secondaries swallow failures so a wedged sidecar never stalls the
    main IO path.  Used by the PTY child so every dispatcher emit lands
    on stdio (Ink) AND on a back-WS feeding the dashboard sidebar.
    """

    __slots__ = ("_primary", "_secondaries")

    def __init__(self, primary: "Transport", *secondaries: "Transport") -> None:
        self._primary = primary
        self._secondaries = secondaries

    def write(self, obj: dict) -> bool:
        # Primary first so a slow sidecar (WS publisher) never delays Ink/stdio.
        ok = self._primary.write(obj)
        for sec in self._secondaries:
            try:
                sec.write(obj)
            except Exception:
                pass
        return ok

    def close(self) -> None:
        try:
            self._primary.close()
        finally:
            for sec in self._secondaries:
                try:
                    sec.close()
                except Exception:
                    pass
