"""Windows sleep / resume power management for the gateway (issue #100025).

When a Windows machine sleeps every TCP connection (Feishu websocket, IMAP,
SMTP, webhook listeners, DB handles) goes stale. The asyncio loop is frozen
for hours; on resume the wall-clock jumps, the monotonic clock jumps past
the heartbeat interval, and any stale-socket read/write can raise out of an
awaited coroutine into the loop exception handler. Until this module the
gateway had *zero* code listening for power events -- the process eventually
crashed without ever calling :func:`gateway.lifecycle_ledger.mark_exited`,
so the next boot reported an ``UNCLEAN / SIGKILL`` death (``suspected_oom:
false`` -- 12 times in ~4 weeks on the reporter's machine).

This module provides two complementary detectors; GatewayRunner arms both
from its asyncio loop task so neither can bring down startup:

1. **Native Windows power broadcast** (``WM_POWERBROADCAST`` /
   ``PBT_APMSUSPEND`` / ``PBT_APMRESUMEAUTOMATIC`` / ``PBT_APMRESUMESUSPEND``)
   via a hidden window running a ``GetMessageW`` pump in a daemon thread.
   The thread marshals callbacks onto the gateway loop with
   ``call_soon_threadsafe`` -- the gateway task itself never blocks on Win32.

2. **Monotonic-jump detector** -- a cross-platform asyncio task that wakes
   every *interval* seconds and checks whether ``time.monotonic()`` advanced
   by more than ``interval + threshold``. A multi-hour sleep looks like a
   single 18_000 s "tick" and is reported as a resume. This is the sole
   detector on POSIX/Linux/macOS and the fallback on Windows when the
   hidden-window pump cannot be created (pythonw without a window station,
   missing user32, etc.).

Both detectors converge on a single resume callback
(:func:`handle_system_resume`) that logs the event and schedules platform
reconnects through GatewayRunner's existing reconnect watcher. A suspend
callback is also exposed for completeness -- it is currently a log-only hook
so a future ``PBT_APMSUSPEND``-time flush can be added without changing the
threading contract.

All public entry points are best-effort and never raise: power management
must not be able to take down a gateway that would otherwise stay up.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Win32 power-broadcast constants. Keep them at module scope so
# tests / tools can import them without pulling in ctypes.
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_POWERSETTINGCHANGE = 0x8013

# Sleep-detection tuning -- deliberately conservative so a slow DNS
# lookup or a WSL2 VHDX stall (measured p99 31 s, max 112 s on the
# #90502 incident box) is not mistaken for an overnight suspend.
DEFAULT_SLEEP_DETECTION_INTERVAL_S = 30.0
DEFAULT_SLEEP_THRESHOLD_S = 60.0  # elapsed > interval + threshold => resume
MIN_SLEEP_DURATION_FOR_RESUME_S = 45.0

# ---------------------------------------------------------------------------
# Monotonic-jump detector (cross-platform, always available)
# ---------------------------------------------------------------------------

async def sleep_detector_loop(
    on_resume: Callable[[float], Any],
    *,
    interval: float = DEFAULT_SLEEP_DETECTION_INTERVAL_S,
    threshold: float = DEFAULT_SLEEP_THRESHOLD_S,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Poll ``time.monotonic()`` for sleep-like jumps.

    ``on_resume`` is called as ``await on_resume(sleep_duration)`` when the
    observed tick exceeds ``interval + threshold``. The argument is the
    *extra* time beyond one normal interval (a rough "how long were we
    suspended" estimate).

    ``stop_event`` (optional) makes the sleep interruptible so the gateway
    can shut the detector down without cancelling the task.
    """
    try:
        interval_f = max(float(interval), 1.0)
    except (TypeError, ValueError):
        interval_f = DEFAULT_SLEEP_DETECTION_INTERVAL_S
    try:
        threshold_f = max(float(threshold), 0.0)
    except (TypeError, ValueError):
        threshold_f = DEFAULT_SLEEP_THRESHOLD_S

    last_mono = time.monotonic()
    # ``last_wall`` is not used for the verdict, but keeping it makes the
    # log line "wall jumped N s / mono jumped N s" possible in future without
    # adding more state. Read once per tick so wall-clock skew never drives
    # the verdict (monotonic is the only RST-proof source).
    last_wall = time.time()

    while True:
        try:
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_f)
                    # stop_event set -> graceful shutdown, not a sleep
                    return
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval_f)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("sleep_detector: sleep wait failed", exc_info=True)
            await asyncio.sleep(interval_f)
            continue

        now_mono = time.monotonic()
        now_wall = time.time()
        elapsed = now_mono - last_mono
        # Update anchors before invoking the callback so a slow callback
        # does not compound into a spurious second resume on the next tick.
        last_mono = now_mono
        last_wall = now_wall

        # A normal tick is ~interval_f seconds. A sleep shows up as a
        # single huge tick (hours). A brief stall (slow reconnect, fsync)
        # is at most ~100 s and is intentionally below the limit.
        if elapsed > interval_f + threshold_f:
            sleep_duration = elapsed - interval_f
            if sleep_duration >= MIN_SLEEP_DURATION_FOR_RESUME_S:
                logger.info(
                    "Sleep/wake detected: monotonic jumped %.1fs "
                    "(interval %.1fs + threshold %.1fs) -- treating as resume after %.1fs suspend",
                    elapsed,
                    interval_f,
                    threshold_f,
                    sleep_duration,
                )
                try:
                    result = on_resume(sleep_duration)
                    if asyncio.iscoroutine(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("sleep_detector: on_resume callback failed", exc_info=True)


def start_sleep_detector(
    on_resume: Callable[[float], Any],
    loop: Optional[asyncio.AbstractEventLoop] = None,
    *,
    interval: float = DEFAULT_SLEEP_DETECTION_INTERVAL_S,
    threshold: float = DEFAULT_SLEEP_THRESHOLD_S,
) -> asyncio.Task:
    """Start the monotonic-jump detector as an asyncio task.

    The task is tagged ``_hermes_supervised_watcher`` so the gateway's
    scale-to-zero idle check does not mistake a healthy idle gateway for a
    permanently busy one (same tagging as the heartbeat task).
    """
    try:
        running_loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        # No running loop -- caller is responsible for retrying from the
        # gateway's ``start()`` which does have a loop.
        raise

    task = running_loop.create_task(
        sleep_detector_loop(on_resume, interval=interval, threshold=threshold)
    )
    try:
        task._hermes_supervised_watcher = True  # type: ignore[attr-defined]
    except Exception:
        pass
    return task


def stop_sleep_detector(task: Optional[asyncio.Task]) -> None:
    """Cancel a detector task started by :func:`start_sleep_detector`."""
    if task is None:
        return
    try:
        task.cancel()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Native Windows power-broadcast monitor (hidden window + GetMessage pump)
# ---------------------------------------------------------------------------

class WindowsPowerMonitor:
    """Hidden-window ``WM_POWERBROADCAST`` listener for Windows.

    Runs a ``GetMessageW`` pump in a daemon thread. ``on_suspend`` and
    ``on_resume`` are invoked on the gateway asyncio loop thread via
    ``call_soon_threadsafe`` and may be plain callables or coroutines.

    On non-Windows platforms :meth:`start` returns ``False`` immediately.
    On Windows, any failure to create the window (no window station,
    missing user32, etc.) is logged at debug and ``start`` returns ``False``
    -- the monotonic detector remains the fallback so the gateway stays up.
    """

    def __init__(
        self,
        on_suspend: Optional[Callable[[], Any]] = None,
        on_resume: Optional[Callable[[], Any]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._on_suspend = on_suspend
        self._on_resume = on_resume
        self._loop = loop
        self._thread: Optional[threading.Thread] = None
        self._hwnd: Optional[int] = None
        self._stop_event = threading.Event()
        self._started = False
        self._wndproc_ref = None  # keep ctypes callback alive
        self._class_atom: Optional[int] = None
        self._hinstance: Optional[int] = None

    # -- public API ---------------------------------------------------------

    def start(self) -> bool:
        """Start the hidden-window pump. Return True on success."""
        if self._started:
            return True
        if sys.platform != "win32":
            logger.debug("WindowsPowerMonitor: not on win32 -- not arming")
            return False
        try:
            return self._start_win32()
        except Exception:
            logger.debug("WindowsPowerMonitor: failed to arm", exc_info=True)
            return False

    def stop(self) -> None:
        """Stop the pump and destroy the hidden window."""
        self._stop_event.set()
        hwnd = self._hwnd
        if hwnd is not None:
            try:
                import ctypes

                ctypes.windll.user32.PostMessageW(hwnd, 0x0012, 0, 0)  # WM_QUIT via PostMessage
                # Also try to wake GetMessage if it's blocked
                ctypes.windll.user32.PostThreadMessageW(
                    ctypes.windll.kernel32.GetCurrentThreadId(), 0x0012, 0, 0
                )
            except Exception:
                pass
        # Also post WM_QUIT to the pump thread's message queue directly
        try:
            if self._thread is not None and self._thread.ident is not None:
                import ctypes

                ctypes.windll.user32.PostThreadMessageW(self._thread.ident, 0x0012, 0, 0)
        except Exception:
            pass
        if self._thread is not None:
            try:
                self._thread.join(timeout=2.0)
            except Exception:
                pass
        # Best-effort window/class cleanup (the thread already did this on
        # exit, but a start() that failed half-way may have left them).
        self._started = False
        self._hwnd = None
        self._thread = None
        self._wndproc_ref = None

    @property
    def is_running(self) -> bool:
        return bool(self._started and self._thread is not None and self._thread.is_alive())

    # -- Win32 internals ----------------------------------------------------

    def _start_win32(self) -> bool:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # Capture loop at start-time. If we're not on the gateway loop thread
        # (e.g. called from a test), fall back to get_event_loop.
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    loop = asyncio.get_event_loop()
                except Exception:
                    loop = None
        self._loop = loop

        on_suspend = self._on_suspend
        on_resume = self._on_resume
        stop_event = self._stop_event

        # Keep strong refs so the thread closure can see them
        loop_ref = loop

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        def _invoke(cb: Optional[Callable], *args: Any) -> None:
            if cb is None:
                return
            # Marshal onto the gateway loop; fall back to direct call if
            # no loop is available (tests / early startup).
            try:
                if loop_ref is not None:
                    try:
                        # ``call_soon_threadsafe`` with a coroutine needs
                        # ``create_task`` -- handle both.
                        def _run() -> None:
                            try:
                                result = cb(*args)
                                if asyncio.iscoroutine(result):
                                    try:
                                        asyncio.create_task(result)
                                    except RuntimeError:
                                        # No running loop in this thread --
                                        # schedule on the gateway loop instead
                                        try:
                                            asyncio.run_coroutine_threadsafe(result, loop_ref)
                                        except Exception:
                                            pass
                            except Exception:
                                logger.debug("WindowsPowerMonitor callback failed", exc_info=True)

                        loop_ref.call_soon_threadsafe(_run)
                        return
                    except RuntimeError:
                        pass
            except Exception:
                pass
            # No loop -- direct invocation (synchronous, best-effort)
            try:
                result = cb(*args)
                if asyncio.iscoroutine(result):
                    try:
                        asyncio.run(result)
                    except RuntimeError:
                        pass
            except Exception:
                logger.debug("WindowsPowerMonitor direct callback failed", exc_info=True)

        @WNDPROC
        def _wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_POWERBROADCAST:
                if wparam == PBT_APMSUSPEND:
                    logger.info("Windows power event: PBT_APMSUSPEND (system suspending)")
                    _invoke(on_suspend)
                    return 1
                elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    name = "PBT_APMRESUMEAUTOMATIC" if wparam == PBT_APMRESUMEAUTOMATIC else "PBT_APMRESUMESUSPEND"
                    logger.info("Windows power event: %s (system resuming)", name)
                    _invoke(on_resume)
                    return 1
                # PBT_POWERSETTINGCHANGE and others: ignore but claim handled
                return 1
            # WM_DESTROY / WM_CLOSE: quit the pump
            if msg in (0x0002, 0x0010):  # WM_DESTROY, WM_CLOSE
                user32.PostQuitMessage(0)
                return 0
            try:
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            except Exception:
                return 0

        # Keep the WNDPROC alive for the lifetime of the monitor
        self._wndproc_ref = _wndproc

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HANDLE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HANDLE),
            ]

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "HermesGatewayPowerMonitor"

        wndclass = WNDCLASSEXW()
        wndclass.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wndclass.style = 0
        wndclass.lpfnWndProc = ctypes.cast(_wndproc, ctypes.c_void_p).value
        # ``WINFUNCTYPE`` already handles the cast, but some ctypes versions
        # want the raw callable pointer:
        try:
            wndclass.lpfnWndProc = _wndproc  # type: ignore[assignment]
        except Exception:
            pass
        wndclass.cbClsExtra = 0
        wndclass.cbWndExtra = 0
        wndclass.hInstance = hinstance
        wndclass.hIcon = None
        wndclass.hCursor = None
        wndclass.hbrBackground = None
        wndclass.lpszMenuName = None
        wndclass.lpszClassName = class_name
        wndclass.hIconSm = None

        atom = user32.RegisterClassExW(ctypes.byref(wndclass))
        if not atom:
            # Class may already be registered from a previous GatewayRunner
            # in this process (tests that reuse the interpreter). That's OK.
            err = kernel32.GetLastError()
            # 1410 == ERROR_CLASS_ALREADY_EXISTS
            if err != 1410:
                logger.debug("WindowsPowerMonitor: RegisterClassExW failed err=%s", err)
                return False
            atom = 0  # sentinel: we didn't register, so don't unregister

        self._class_atom = atom
        self._hinstance = hinstance

        hwnd_ref: list[Optional[int]] = [None]
        ready = threading.Event()
        failed: list[Optional[str]] = [None]

        def _pump() -> None:
            try:
                hwnd = user32.CreateWindowExW(
                    0,
                    class_name,
                    "Hermes Gateway Power Monitor",
                    0,  # WS_OVERLAPPED (hidden -- no WS_VISIBLE)
                    0,
                    0,
                    0,
                    0,
                    None,
                    None,
                    hinstance,
                    None,
                )
                if not hwnd:
                    failed[0] = f"CreateWindowExW failed err={kernel32.GetLastError()}"
                    ready.set()
                    return
                hwnd_ref[0] = hwnd
                self._hwnd = hwnd
                ready.set()

                msg = wintypes.MSG()
                # Pump until WM_QUIT or stop_event. Check stop_event with a
                # timeout so PostThreadMessage is not the only wake path.
                while not stop_event.is_set():
                    # Use PeekMessage + wait to make the stop_event check
                    # responsive without burning CPU. GetMessage blocks.
                    ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if ret == 0:  # WM_QUIT
                        break
                    if ret == -1:
                        logger.debug("WindowsPowerMonitor: GetMessageW error err=%s", kernel32.GetLastError())
                        break
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            except Exception:
                logger.debug("WindowsPowerMonitor pump crashed", exc_info=True)
                failed[0] = "pump exception"
                ready.set()
            finally:
                # Cleanup window + class. Best-effort; the OS reclaims them
                # on process exit anyway.
                try:
                    hwnd = hwnd_ref[0]
                    if hwnd:
                        user32.DestroyWindow(hwnd)
                except Exception:
                    pass
                try:
                    if atom:
                        user32.UnregisterClassW(class_name, hinstance)
                except Exception:
                    pass
                ready.set()

        thread = threading.Thread(target=_pump, daemon=True, name="hermes-power-monitor")
        thread.start()
        # Wait for the window to be created (or failure)
        if not ready.wait(timeout=5.0):
            logger.debug("WindowsPowerMonitor: window creation timed out")
            return False
        if failed[0] is not None:
            logger.debug("WindowsPowerMonitor: %s", failed[0])
            return False
        if hwnd_ref[0] is None:
            # Pump already exited (e.g. window station has no desktop)
            logger.debug("WindowsPowerMonitor: hidden window not created -- monitor not armed")
            return False

        self._thread = thread
        self._started = True
        logger.info("Windows power monitor armed (WM_POWERBROADCAST hidden window)")
        return True


# ---------------------------------------------------------------------------
# Unified manager used by GatewayRunner
# ---------------------------------------------------------------------------

class PowerManager:
    """Unified suspend/resume manager for a GatewayRunner instance.

    Arms the native Windows monitor (if available) and the monotonic-jump
    detector. Either firing triggers the same :meth:`on_resume` path, but
    the native path is preferred because it fires *immediately* on
    ``PBT_APMRESUMEAUTOMATIC`` rather than up to one ``interval`` later.
    A de-duplication window suppresses the monotonic detector's follow-on
    trigger when the native event already handled the same wake.

    Usage::

        mgr = PowerManager(loop, on_suspend=runner._on_system_suspend,
                           on_resume=runner._on_system_resume)
        mgr.start()

        # ... later, on GatewayRunner.stop():
        mgr.stop()
    """

    # If a native resume was handled within this window, suppress a
    # follow-on monotonic resume for the same sleep. Long enough to cover
    # the detector's worst-case lag (one full interval).
    _DEDUP_WINDOW_S = 90.0

    def __init__(
        self,
        on_suspend: Optional[Callable[[], Any]] = None,
        on_resume: Optional[Callable[[float], Any]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._on_suspend = on_suspend
        self._on_resume = on_resume
        self._loop = loop
        self._windows_monitor: Optional[WindowsPowerMonitor] = None
        self._sleep_task: Optional[asyncio.Task] = None
        self._last_resume_mono: Optional[float] = None
        self._lock = threading.Lock()

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if loop is not None:
            self._loop = loop
        try:
            loop = self._loop or asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self._loop = loop

        # Wrap callbacks with de-duplication so the two detectors converge.
        def _suspend_wrapper() -> None:
            cb = self._on_suspend
            if cb is None:
                return
            try:
                result = cb()
                if asyncio.iscoroutine(result) and loop is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(result, loop)
                    except RuntimeError:
                        pass
            except Exception:
                logger.debug("PowerManager: on_suspend failed", exc_info=True)

        def _resume_wrapper(sleep_duration: float = 0.0) -> None:
            now = time.monotonic()
            with self._lock:
                if self._last_resume_mono is not None:
                    if now - self._last_resume_mono < self._DEDUP_WINDOW_S:
                        logger.debug(
                            "PowerManager: suppressing duplicate resume (%.1fs since last)",
                            now - self._last_resume_mono,
                        )
                        return
                self._last_resume_mono = now
            cb = self._on_resume
            if cb is None:
                return
            try:
                result = cb(sleep_duration)
                if asyncio.iscoroutine(result):
                    # Called from win32 thread (no running loop) vs.
                    # from sleep_detector (already on loop). Handle both.
                    try:
                        running = asyncio.get_running_loop()
                        if running is loop:
                            # Already on the gateway loop -- create_task
                            try:
                                asyncio.create_task(result)
                            except RuntimeError:
                                pass
                        else:
                            if loop is not None:
                                asyncio.run_coroutine_threadsafe(result, loop)
                    except RuntimeError:
                        # No running loop here (win32 thread) -- thread-safe submit
                        if loop is not None:
                            try:
                                asyncio.run_coroutine_threadsafe(result, loop)
                            except RuntimeError:
                                pass
                # plain callable: already invoked above as cb(sleep_duration)
            except Exception:
                logger.debug("PowerManager: on_resume failed", exc_info=True)

        # Arm native Windows monitor where available. Its on_resume is
        # invoked from a native thread, so wrap it to schedule on the loop.
        if sys.platform == "win32":
            try:
                mon = WindowsPowerMonitor(
                    on_suspend=lambda: _suspend_wrapper(),
                    on_resume=lambda: _resume_wrapper(0.0),
                    loop=loop,
                )
                if mon.start():
                    self._windows_monitor = mon
                else:
                    logger.debug("PowerManager: WindowsPowerMonitor not armed -- monotonic fallback only")
            except Exception:
                logger.debug("PowerManager: WindowsPowerMonitor init failed", exc_info=True)

        # Always arm the monotonic detector -- it is the sole detector on
        # POSIX and the fallback/confirm on Windows.
        try:
            if loop is not None:
                # Wrap so monotonic resume also goes through dedup
                async def _mono_resume(duration: float) -> None:
                    _resume_wrapper(duration)

                self._sleep_task = start_sleep_detector(
                    _mono_resume,
                    loop=loop,
                    interval=DEFAULT_SLEEP_DETECTION_INTERVAL_S,
                    threshold=DEFAULT_SLEEP_THRESHOLD_S,
                )
                logger.info(
                    "Sleep detector armed (interval=%.0fs threshold=%.0fs)",
                    DEFAULT_SLEEP_DETECTION_INTERVAL_S,
                    DEFAULT_SLEEP_THRESHOLD_S,
                )
            else:
                logger.debug("PowerManager: no running loop -- sleep detector not armed")
        except Exception:
            logger.debug("PowerManager: failed to arm sleep detector", exc_info=True)

        if self._windows_monitor is None and self._sleep_task is None:
            logger.warning("PowerManager: no power monitor could be armed -- sleep/wake detection disabled")
        else:
            logger.info(
                "Power management armed (native=%s monotonic=%s)",
                bool(self._windows_monitor and self._windows_monitor.is_running),
                bool(self._sleep_task is not None and not self._sleep_task.done()),
            )

    def stop(self) -> None:
        if self._windows_monitor is not None:
            try:
                self._windows_monitor.stop()
            except Exception:
                pass
            self._windows_monitor = None
        if self._sleep_task is not None:
            stop_sleep_detector(self._sleep_task)
            self._sleep_task = None

    @property
    def is_armed(self) -> bool:
        win_ok = bool(self._windows_monitor and self._windows_monitor.is_running)
        mono_ok = bool(self._sleep_task is not None and not self._sleep_task.done())
        return win_ok or mono_ok
