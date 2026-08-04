"""Cross-process FIFO admission for capacity-constrained local endpoints.

The lane is intentionally provider-agnostic.  Callers own the context for the
entire request lifetime, including stream consumption, so a single-capacity
server never receives overlapping work from separate Hermes processes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import AsyncIterator, Callable, Iterator
from urllib.parse import urlparse
import uuid

import psutil

from agent.model_metadata import is_local_endpoint


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _default_state_root() -> Path:
    """Return one per-user runtime root shared by all Hermes profiles."""

    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return Path(tempfile.gettempdir()) / f"hermes-local-endpoint-lanes{suffix}"


@dataclass(frozen=True)
class LocalEndpointLease:
    """Metadata for one acquired endpoint lane."""

    wait_seconds: float
    coordinated: bool
    lane_id: str | None


def _endpoint_origin(base_url: str) -> str:
    value = base_url.strip()
    parsed = urlparse(value if "://" in value else f"http://{value}")
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS or host.startswith("127."):
        host = "loopback"
    scheme = (parsed.scheme or "http").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{port}"


def lane_dir_for_endpoint(
    base_url: str,
    *,
    state_root: Path | None = None,
) -> Path:
    """Return a privacy-preserving, origin-scoped directory for *base_url*."""

    root = state_root or _default_state_root()
    lane_id = hashlib.sha256(_endpoint_origin(base_url).encode("utf-8")).hexdigest()[:24]
    return Path(root) / lane_id


def _write_ticket(path: Path) -> None:
    payload = {
        "pid": os.getpid(),
        "thread_id": threading.get_ident(),
        "process_created": psutil.Process().create_time(),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _ticket_owner_is_alive(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    try:
        pid = int(payload["pid"])
        thread_id = int(payload["thread_id"])
        process_created = float(payload["process_created"])
    except (KeyError, TypeError, ValueError):
        return False

    if pid == os.getpid():
        return any(thread.ident == thread_id for thread in threading.enumerate())

    try:
        process = psutil.Process(pid)
        return abs(process.create_time() - process_created) < 0.01
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True


def _prune_stale_tickets(lane_dir: Path, stale_after_s: float) -> None:
    cutoff = time.time() - stale_after_s
    for ticket in lane_dir.glob("ticket-*.json"):
        try:
            if ticket.stat().st_mtime > cutoff:
                continue
            payload = json.loads(ticket.read_text(encoding="utf-8"))
            if not _ticket_owner_is_alive(payload):
                ticket.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            try:
                if ticket.stat().st_mtime <= cutoff:
                    ticket.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


def _try_lock(handle) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _open_lock(path: Path):
    handle = path.open("a+b")
    if os.name == "nt" and path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    return handle


@contextmanager
def local_endpoint_lane(
    base_url: str,
    *,
    state_root: Path | None = None,
    enabled: bool = True,
    timeout_s: float | None = None,
    poll_interval_s: float = 0.05,
    stale_after_s: float = 30.0,
    cancel_check: Callable[[], bool] | None = None,
) -> Iterator[LocalEndpointLease]:
    """Acquire a deterministic FIFO lane for a local endpoint.

    Remote endpoints and explicitly disabled lanes are no-ops.  A waiting
    caller may supply ``cancel_check`` or ``timeout_s``; both remove its ticket
    before propagating an exception.
    """

    if not enabled or not is_local_endpoint(base_url):
        yield LocalEndpointLease(wait_seconds=0.0, coordinated=False, lane_id=None)
        return
    if poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be greater than zero")
    if stale_after_s <= 0:
        raise ValueError("stale_after_s must be greater than zero")
    if timeout_s is not None and timeout_s < 0:
        raise ValueError("timeout_s cannot be negative")

    lane_dir = lane_dir_for_endpoint(base_url, state_root=state_root)
    lane_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lane_id = lane_dir.name
    ticket = lane_dir / (
        f"ticket-{time.time_ns():020d}-{os.getpid():010d}-{uuid.uuid4().hex}.json"
    )
    _write_ticket(ticket)
    started = time.monotonic()
    lock_handle = None
    acquired = False

    try:
        while True:
            if cancel_check is not None and cancel_check():
                raise InterruptedError("local endpoint lane wait cancelled")
            elapsed = time.monotonic() - started
            if timeout_s is not None and elapsed >= timeout_s:
                raise TimeoutError(
                    f"timed out after {elapsed:.3f}s waiting for local endpoint lane"
                )

            _prune_stale_tickets(lane_dir, stale_after_s)
            tickets = sorted(lane_dir.glob("ticket-*.json"))
            if tickets and tickets[0] == ticket:
                if lock_handle is None:
                    lock_handle = _open_lock(lane_dir / "lane.lock")
                if _try_lock(lock_handle):
                    acquired = True
                    ticket.unlink(missing_ok=True)
                    break

            try:
                os.utime(ticket, None)
            except FileNotFoundError:
                raise RuntimeError("local endpoint lane ticket disappeared") from None
            time.sleep(poll_interval_s)

        yield LocalEndpointLease(
            wait_seconds=time.monotonic() - started,
            coordinated=True,
            lane_id=lane_id,
        )
    finally:
        ticket.unlink(missing_ok=True)
        if lock_handle is not None:
            try:
                if acquired:
                    _unlock(lock_handle)
            finally:
                lock_handle.close()


@asynccontextmanager
async def async_local_endpoint_lane(
    base_url: str,
    *,
    state_root: Path | None = None,
    enabled: bool = True,
    timeout_s: float | None = None,
    poll_interval_s: float = 0.05,
    stale_after_s: float = 30.0,
    cancel_check: Callable[[], bool] | None = None,
) -> AsyncIterator[LocalEndpointLease]:
    """Async, cancellation-safe mirror of :func:`local_endpoint_lane`.

    File waiting runs off the event-loop thread.  If the coroutine is
    cancelled while queued, cancellation is signalled to that worker and its
    cleanup is awaited before :class:`asyncio.CancelledError` is propagated.
    """

    cancelled = threading.Event()

    def should_cancel() -> bool:
        return cancelled.is_set() or (
            cancel_check is not None and cancel_check()
        )

    manager = local_endpoint_lane(
        base_url,
        state_root=state_root,
        enabled=enabled,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        stale_after_s=stale_after_s,
        cancel_check=should_cancel,
    )
    enter_task = asyncio.create_task(asyncio.to_thread(manager.__enter__))
    entered = False
    lease: LocalEndpointLease | None = None
    try:
        try:
            lease = await asyncio.shield(enter_task)
            entered = True
        except asyncio.CancelledError:
            cancelled.set()
            try:
                lease = await enter_task
                entered = True
            except InterruptedError:
                pass
            if entered:
                await asyncio.to_thread(manager.__exit__, None, None, None)
                entered = False
            raise

        try:
            yield lease
        except BaseException as exc:
            await asyncio.to_thread(
                manager.__exit__,
                type(exc),
                exc,
                exc.__traceback__,
            )
            entered = False
            raise
        else:
            await asyncio.to_thread(manager.__exit__, None, None, None)
            entered = False
    finally:
        cancelled.set()
        if entered:
            await asyncio.to_thread(manager.__exit__, None, None, None)


__all__ = [
    "LocalEndpointLease",
    "async_local_endpoint_lane",
    "lane_dir_for_endpoint",
    "local_endpoint_lane",
]
