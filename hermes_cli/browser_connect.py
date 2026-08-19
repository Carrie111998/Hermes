"""Shared helpers for attaching Hermes to a local browser backend.

Covers Chromium-family CDP launch/discovery and Camofox autodetection used
by both the classic CLI ``/browser connect`` path and TUI/Desktop
``browser.manage``.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import posixpath
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


DEFAULT_BROWSER_CDP_PORT = 9222
DEFAULT_BROWSER_CDP_URL = f"http://127.0.0.1:{DEFAULT_BROWSER_CDP_PORT}"

# In-process /browser connect override. Camofox snapshots must survive a
# connect to the already-configured URL so disconnect can restore it.
BROWSER_CONNECT_MODE_ENV = "BROWSER_CONNECT_MODE"
BROWSER_PREV_CAMOFOX_URL_ENV = "BROWSER_PREV_CAMOFOX_URL"
BROWSER_PREV_CAMOFOX_SET_ENV = "BROWSER_PREV_CAMOFOX_SET"


def normalize_browser_connect_endpoint(endpoint: str) -> str:
    """Normalize user-supplied ``/browser connect`` endpoints.

    Schemeless ``host:port`` inputs become ``http://host:port``. Trailing
    slashes are stripped so later ``/json/version`` and ``/health`` probes
    do not double up path separators.
    """
    raw = (endpoint or "").strip().rstrip("/")
    if not raw:
        return raw
    if "://" in raw:
        return raw
    return f"http://{raw}"


def _http_json(url: str, timeout: float) -> dict | None:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if not (200 <= getattr(resp, "status", 200) < 300):
                return None
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def detect_browser_connect_backend(
    endpoint: str, *, timeout: float = 2.0
) -> tuple[str, str]:
    """Classify a ``/browser connect`` target as ``cdp``, ``camofox``, or ``unknown``.

    CDP is probed first (``/json/version`` ``webSocketDebuggerUrl``) so a
    Chromium-family browser is never mistaken for Camofox. Camofox is the
    fallback when that probe fails and ``/health`` returns HTTP 200.
    """
    raw = normalize_browser_connect_endpoint(endpoint)
    if not raw:
        return "unknown", raw

    parsed = urlparse(raw)
    if parsed.path.startswith("/devtools/browser/"):
        return "cdp", raw

    discovery_url = raw
    if parsed.scheme in {"ws", "wss"}:
        # Bare ``ws://host:port`` can still be a discovery root. A concrete
        # DevTools websocket already returned above.
        if parsed.path and parsed.path != "/":
            return "cdp", raw
        discovery_url = raw.replace("wss://", "https://", 1).replace("ws://", "http://", 1)

    version_url = (
        discovery_url
        if discovery_url.lower().endswith("/json/version")
        else f"{discovery_url.rstrip('/')}/json/version"
    )
    version = _http_json(version_url, timeout)
    if str((version or {}).get("webSocketDebuggerUrl") or "").strip():
        return "cdp", raw

    health_root = discovery_url
    if health_root.lower().endswith("/json/version"):
        health_root = health_root[: -len("/json/version")]
    # Require a JSON object so a generic HTTP 200 (or a CDP probe stub that
    # only exposes ``status``) is not treated as Camofox.
    health = _http_json(f"{health_root.rstrip('/')}/health", timeout)
    if health is not None:
        return "camofox", raw

    return "unknown", raw


def _snapshot_camofox_url() -> None:
    """Remember the exact current ``CAMOFOX_URL`` env state, including identity."""
    if "CAMOFOX_URL" in os.environ:
        os.environ[BROWSER_PREV_CAMOFOX_URL_ENV] = os.environ["CAMOFOX_URL"]
        os.environ[BROWSER_PREV_CAMOFOX_SET_ENV] = "1"
        return
    os.environ.pop(BROWSER_PREV_CAMOFOX_URL_ENV, None)
    os.environ.pop(BROWSER_PREV_CAMOFOX_SET_ENV, None)


def _restore_camofox_url() -> None:
    """Restore the snapshotted ``CAMOFOX_URL``, or unset it when none existed."""
    was_set = os.environ.pop(BROWSER_PREV_CAMOFOX_SET_ENV, "")
    previous = os.environ.pop(BROWSER_PREV_CAMOFOX_URL_ENV, "")
    if was_set:
        os.environ["CAMOFOX_URL"] = previous
        return
    os.environ.pop("CAMOFOX_URL", None)


def apply_browser_backend_override(*, mode: str, url: str) -> None:
    """Publish a reversible ``/browser connect`` backend override.

    Camofox connects always snapshot the prior ``CAMOFOX_URL`` — even when
    the new URL is identical — so disconnect can restore that exact state.
    CDP connects leave ``CAMOFOX_URL`` in place; ``BROWSER_CDP_URL`` already
    suppresses Camofox routing.
    """
    normalized = (url or "").strip().rstrip("/")
    if mode == "camofox":
        _snapshot_camofox_url()
        os.environ["CAMOFOX_URL"] = normalized
        os.environ[BROWSER_CONNECT_MODE_ENV] = "camofox"
        os.environ.pop("BROWSER_CDP_URL", None)
        return

    os.environ["BROWSER_CDP_URL"] = url
    os.environ[BROWSER_CONNECT_MODE_ENV] = "cdp"


def restore_browser_backend_override() -> str:
    """Clear the connect override and restore any snapshotted Camofox env.

    Returns the mode that was active (``cdp``, ``camofox``, or ``""``).
    """
    mode = os.environ.pop(BROWSER_CONNECT_MODE_ENV, "").strip().lower()
    os.environ.pop("BROWSER_CDP_URL", None)
    if mode == "camofox" or BROWSER_PREV_CAMOFOX_SET_ENV in os.environ:
        _restore_camofox_url()
    else:
        os.environ.pop(BROWSER_PREV_CAMOFOX_URL_ENV, None)
        os.environ.pop(BROWSER_PREV_CAMOFOX_SET_ENV, None)
    return mode


def get_browser_connect_override() -> tuple[str, str]:
    """Return ``(mode, url)`` for the active in-process connect override.

    Does no network I/O. An empty mode means no ``/browser connect`` override
    is active (configured ``CAMOFOX_URL`` / ``browser.cdp_url`` may still apply).
    """
    mode = os.environ.get(BROWSER_CONNECT_MODE_ENV, "").strip().lower()
    if mode == "camofox":
        return "camofox", os.environ.get("CAMOFOX_URL", "").strip().rstrip("/")
    cdp = os.environ.get("BROWSER_CDP_URL", "").strip()
    if cdp or mode == "cdp":
        return "cdp", cdp
    return "", ""


_DARWIN_APPS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

_WINDOWS_BROWSER_GROUPS = (
    (("chrome.exe", "chrome"), (("Google", "Chrome", "Application", "chrome.exe"),)),
    (
        ("chromium.exe", "chromium"),
        (("Chromium", "Application", "chrome.exe"), ("Chromium", "Application", "chromium.exe")),
    ),
    (("brave.exe", "brave"), (("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),)),
    (("msedge.exe", "msedge"), (("Microsoft", "Edge", "Application", "msedge.exe"),)),
)

_WINDOWS_BIN_NAMES = tuple(name for names, _ in _WINDOWS_BROWSER_GROUPS for name in names)
_WINDOWS_INSTALL_PARTS = tuple(parts for _, group in _WINDOWS_BROWSER_GROUPS for parts in group)

_LINUX_BROWSER_GROUPS = (
    (
        ("google-chrome", "google-chrome-stable"),
        ("/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"),
    ),
    (
        ("chromium-browser", "chromium"),
        ("/usr/bin/chromium-browser", "/usr/bin/chromium"),
    ),
    (
        ("brave-browser", "brave-browser-stable", "brave"),
        (
            "/usr/bin/brave-browser",
            "/usr/bin/brave-browser-stable",
            "/usr/bin/brave",
            "/snap/bin/brave",
            "/opt/brave.com/brave/brave-browser",
            "/opt/brave.com/brave/brave",
            "/opt/brave-bin/brave",
        ),
    ),
    (
        ("microsoft-edge", "microsoft-edge-stable", "msedge"),
        (
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/opt/microsoft/msedge/microsoft-edge",
            "/opt/microsoft/msedge/msedge",
        ),
    ),
)

_LINUX_BIN_NAMES = tuple(name for names, _ in _LINUX_BROWSER_GROUPS for name in names)
_LINUX_INSTALL_PATHS = tuple(path for _, paths in _LINUX_BROWSER_GROUPS for path in paths)


def get_chrome_debug_candidates(system: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(path: str | None) -> None:
        if not path:
            return
        normalized = os.path.normcase(os.path.normpath(path))
        if normalized in seen or not os.path.isfile(path):
            return
        candidates.append(path)
        seen.add(normalized)

    def add_windows_install_paths(
        bases: tuple[str | None, ...],
        install_groups: tuple[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]], ...],
    ) -> None:
        for _, group in install_groups:
            for base in filter(None, bases):
                for parts in group:
                    # Only called with WSL ``/mnt/c/...`` bases — those are
                    # POSIX paths regardless of the host OS, so join with
                    # posixpath (os.path.join would emit backslashes on nt).
                    add(posixpath.join(base, *parts))

    if system == "Darwin":
        for app in _DARWIN_APPS:
            add(app)
        return candidates

    if system == "Windows":
        install_bases = (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        )
        for names, install_parts in _WINDOWS_BROWSER_GROUPS:
            for name in names:
                add(shutil.which(name))
            for base in filter(None, install_bases):
                for parts in install_parts:
                    add(os.path.join(base, *parts))
        return candidates

    for names, paths in _LINUX_BROWSER_GROUPS:
        for name in names:
            add(shutil.which(name))
        for path in paths:
            add(path)
    add_windows_install_paths(("/mnt/c/Program Files", "/mnt/c/Program Files (x86)"), _WINDOWS_BROWSER_GROUPS)
    return candidates


def chrome_debug_data_dir() -> str:
    return str(get_hermes_home() / "chrome-debug")


def _chrome_debug_args(port: int) -> list[str]:
    return [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={chrome_debug_data_dir()}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def is_browser_debug_ready(url: str, timeout: float = 1.0) -> bool:
    """Return True when ``url`` exposes a reachable Chrome DevTools endpoint."""
    import socket
    import urllib.request
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"http://{url}")
    try:
        port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    except ValueError:
        return False

    if parsed.scheme in {"ws", "wss"} and parsed.path.startswith("/devtools/browser/"):
        if not parsed.hostname:
            return False
        try:
            with socket.create_connection((parsed.hostname, port), timeout=timeout):
                return True
        except OSError:
            return False

    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    if scheme not in {"http", "https"} or not parsed.netloc:
        return False

    root = f"{scheme}://{parsed.netloc}".rstrip("/")
    for probe in (f"{root}/json/version", f"{root}/json"):
        try:
            with urllib.request.urlopen(probe, timeout=timeout) as resp:
                if 200 <= getattr(resp, "status", 200) < 300:
                    return True
        except Exception:
            continue
    return False


# Both loopback literals: Windows (and some Linux setups) can hand the IPv4
# loopback to one process and the IPv6 loopback to another. Chrome asked to
# bind :9222 while e.g. VS Code's js-debug holds 127.0.0.1:9222 will come up
# on [::1]:9222 only — reachable, but invisible to an IPv4-only probe.
_LOOPBACK_PROBE_HOSTS = ("127.0.0.1", "[::1]")
_LOOPBACK_SOCKET_HOSTS = ("127.0.0.1", "::1")


def discover_local_cdp_url(port: int, timeout: float = 1.0) -> str | None:
    """Return the first loopback URL (IPv4 first, then IPv6) speaking CDP.

    Dual-stack discovery: when another application squats the IPv4
    loopback on ``port``, a debug browser launched with
    ``--remote-debugging-port`` may bind only ``[::1]``. Probing both
    literals finds it either way. Returns ``None`` when neither
    loopback exposes a CDP discovery endpoint.
    """
    for host in _LOOPBACK_PROBE_HOSTS:
        url = f"http://{host}:{port}"
        if is_browser_debug_ready(url, timeout=timeout):
            return url
    return None


def local_port_in_use(port: int, timeout: float = 0.5) -> bool:
    """Return True when either loopback accepts TCP on ``port``.

    Callers use this AFTER a failed CDP probe to distinguish "port is
    free, we can launch a browser on it" from "another application
    (IDE debugger, dev server) is squatting the port and a launch
    would fight it".
    """
    import socket

    for host in _LOOPBACK_SOCKET_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def find_free_debug_port(preferred: int = DEFAULT_BROWSER_CDP_PORT, attempts: int = 10) -> int:
    """Return the first port after ``preferred`` bindable on both loopbacks.

    Used when ``preferred`` is occupied by a non-CDP application: rather
    than launching a browser into a bind conflict, pick a nearby free
    port. Falls back to ``preferred + 1`` if nothing binds (the launch
    will then fail with a clear browser-side error instead of silently
    doing nothing).
    """
    import socket

    for port in range(preferred + 1, preferred + 1 + attempts):
        bindable = True
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            try:
                with socket.socket(family, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((host, port))
            except OSError:
                bindable = False
                break
        if bindable:
            return port
    return preferred + 1


def manual_chrome_debug_command(port: int = DEFAULT_BROWSER_CDP_PORT, system: str | None = None) -> str | None:
    system = system or platform.system()
    candidates = get_chrome_debug_candidates(system)

    if candidates:
        argv = [candidates[0], *_chrome_debug_args(port)]
        return subprocess.list2cmdline(argv) if system == "Windows" else shlex.join(argv)

    if system == "Darwin":
        data_dir = chrome_debug_data_dir()
        return (
            f'open -a "Google Chrome" --args --remote-debugging-port={port} '
            f'--user-data-dir="{data_dir}" --no-first-run --no-default-browser-check'
        )

    return None


def _detach_kwargs(system: str) -> dict:
    if system != "Windows":
        return {"start_new_session": True}
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    return {"creationflags": flags} if flags else {}


def _wait_for_browser_debug_ready_or_exit(
    proc: subprocess.Popen,
    port: int,
    timeout: float = 2.0,
    interval: float = 0.1,
) -> str:
    """Classify a launched browser as ready, exited, or still starting.

    We only need to wait long enough to catch the common failure mode where a
    candidate binary exists but exits immediately before exposing the CDP port.
    Slower browsers can still finish starting after this grace window.
    """
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        # Dual-stack: a squatter on the IPv4 loopback can push the browser
        # to bind [::1] only — check both so a successful launch is seen.
        if discover_local_cdp_url(port, timeout=min(interval, 0.2)):
            return "ready"
        if proc.poll() is not None:
            return "exited"
        time.sleep(interval)

    return "starting"


_LAUNCH_STDERR_LOG = "launch-stderr.log"
_STDERR_TAIL_LIMIT = 2000


@dataclass
class LaunchAttempt:
    """Outcome of one candidate-binary launch attempt."""

    binary: str
    state: str  # "ready" | "starting" | "exited" | "spawn-failed"
    returncode: int | None = None
    stderr_tail: str = ""


@dataclass
class ChromeDebugLaunch:
    """Structured result of ``launch_chrome_debug``.

    ``launched`` mirrors the legacy boolean contract: a launch command was
    executed and the browser is ready or still starting (it does NOT
    guarantee the CDP port ever opens). ``attempts`` carries per-candidate
    diagnostics so callers can explain *why* nothing came up.
    """

    launched: bool = False
    attempts: list[LaunchAttempt] = field(default_factory=list)

    @property
    def hint(self) -> str | None:
        """Best user-facing explanation for a failed/soft launch, if any."""
        for attempt in self.attempts:
            if attempt.state == "exited" and attempt.returncode == 0:
                name = os.path.basename(attempt.binary)
                return (
                    f"{name} exited immediately without opening the debug port — an already-running "
                    f"{name} instance likely absorbed the launch (Chromium's single-instance "
                    "behavior). Close ALL of its processes (including background/tray instances) "
                    "and retry /browser connect."
                )
        for attempt in self.attempts:
            if attempt.state == "exited" and attempt.stderr_tail:
                return (
                    f"{os.path.basename(attempt.binary)} exited before the debug port opened: "
                    f"{attempt.stderr_tail.splitlines()[-1].strip()}"
                )
        return None


def _read_stderr_tail(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        return data[-_STDERR_TAIL_LIMIT:].decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def launch_chrome_debug(
    port: int = DEFAULT_BROWSER_CDP_PORT, system: str | None = None
) -> ChromeDebugLaunch:
    """Launch a Chromium-family browser with remote debugging, with diagnostics.

    Tries each detected candidate binary in turn. A candidate that exits
    before the CDP port opens (crash, singleton forward to an existing
    instance, bad profile dir) is logged — with exit code and a stderr tail —
    and the next candidate is tried.
    """
    system = system or platform.system()
    result = ChromeDebugLaunch()
    candidates = get_chrome_debug_candidates(system)
    if not candidates:
        logger.info("browser debug launch: no Chromium-family binary found (system=%s)", system)
        return result

    data_dir = chrome_debug_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    stderr_path = os.path.join(data_dir, _LAUNCH_STDERR_LOG)

    for candidate in candidates:
        try:
            with open(stderr_path, "wb") as stderr_file:
                proc = subprocess.Popen(
                    [candidate, *_chrome_debug_args(port)],
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    **_detach_kwargs(system),
                )
        except Exception as exc:
            result.attempts.append(LaunchAttempt(binary=candidate, state="spawn-failed"))
            logger.info("browser debug launch: failed to spawn %s: %s", candidate, exc)
            continue

        logger.info(
            "browser debug launch: spawned %s (pid=%s) with --remote-debugging-port=%d",
            candidate,
            getattr(proc, "pid", None),
            port,
        )
        state = _wait_for_browser_debug_ready_or_exit(proc, port)
        attempt = LaunchAttempt(binary=candidate, state=state)
        result.attempts.append(attempt)

        if state != "exited":
            result.launched = True
            return result

        attempt.returncode = getattr(proc, "returncode", None)
        attempt.stderr_tail = _read_stderr_tail(stderr_path)
        logger.warning(
            "browser debug launch: %s exited (code=%s) before port %d opened%s",
            candidate,
            attempt.returncode,
            port,
            f"; stderr tail: {attempt.stderr_tail}" if attempt.stderr_tail else "",
        )

    return result


def try_launch_chrome_debug(port: int = DEFAULT_BROWSER_CDP_PORT, system: str | None = None) -> bool:
    return launch_chrome_debug(port, system).launched
