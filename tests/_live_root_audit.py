"""pytest plugin: record every filesystem *write* that escapes into the real home.

Load it with ``-p tests._live_root_audit`` (the repo root must be importable,
so put it on ``PYTHONPATH``; the ``-p`` form is what ``ops/pytest-run.cmd``
already uses for ``pytest_fd_guard``).

Why an audit hook and not a fixture: a test can leak through any of a dozen
mechanisms -- ``Path.home()``, an import-time ``HERMES_HOME`` snapshot, a
``logging`` handler opened before the fixture ran, a subprocess. Only
``sys.addaudithook`` sees all of them, because it sits under the C
implementations of ``open``/``os.*``/``shutil.*`` rather than beside them.

The hook is *observational* -- it never blocks. Its job is to produce an
attributed list of leaks (nodeid + path + stack) to fix at the source.

Output: JSONL at ``$LIVE_ROOT_AUDIT_OUT`` (default ``live_root_audit.jsonl``
in the cwd), one record per distinct ``(nodeid, event, path)``, written as it
happens so a crashed or truncated run still yields its findings.
"""

from __future__ import annotations

import json
import os
import sys
import threading

# ---------------------------------------------------------------------------
# Snapshot the real home NOW, at import time, before any test mutates the env.
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


_REAL_HOME = _norm(os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.path.expanduser("~"))
_HERMES_ROOT = _norm(os.path.join(_REAL_HOME, ".hermes"))

# Paths that are legitimately written during a test run and are not "the live
# root": the repo checkout under test, the scratch/tmp tree, and the giant
# AppData cache tree (pip/uv/font/MSIX noise, plus %TEMP% itself).
_REPO_ROOT = _norm(os.environ.get("LIVE_ROOT_AUDIT_REPO") or os.getcwd())
_EXCLUDED_PREFIXES: tuple[str, ...] = (
    _REPO_ROOT + os.sep,
    _norm(os.path.join(_REAL_HOME, "appdata")) + os.sep,
)
# Path *components* that are never interesting wherever they appear. Matched as
# whole components: a bare ``os.mkdir(".../__pycache__")`` has no trailing
# separator, so a substring test on ``"\__pycache__\"`` alone misses it and the
# report drowns in bytecode-cache noise.
_EXCLUDED_COMPONENTS = ("__pycache__", ".git", ".pytest_cache", "node_modules")
_EXCLUDED_PARTS = tuple(os.sep + name + os.sep for name in _EXCLUDED_COMPONENTS)
_EXCLUDED_TAILS = tuple(os.sep + name for name in _EXCLUDED_COMPONENTS)

# The shared checkout's virtualenv. Writes here are a real (separately
# documented) hazard -- a test pip-installing into the live venv -- but they are
# high-volume and a different problem, so they get their own severity rather
# than being dropped or mixed into the ~/.hermes findings.
_VENV_ROOT = _norm(os.path.join(_REAL_HOME, ".hermes", "agent-src", ".venv"))

_OUT_PATH = _norm(os.environ.get("LIVE_ROOT_AUDIT_OUT") or os.path.join(os.getcwd(), "live_root_audit.jsonl"))

# ---------------------------------------------------------------------------
# Which audit events represent a write.
# ---------------------------------------------------------------------------

# event name -> indices of its args that are paths
_PATH_EVENTS: dict[str, tuple[int, ...]] = {
    "os.rename": (0, 1),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.link": (0, 1),
    "os.symlink": (0, 1),
    "os.truncate": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.utime": (0,),
    "shutil.copyfile": (0, 1),
    "shutil.copymode": (1,),
    "shutil.copystat": (1,),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
    "shutil.unpack_archive": (1,),
}

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

_state = threading.local()
_lock = threading.Lock()
_seen: set[tuple[str, str, str]] = set()
_records: list[dict] = []

current_nodeid = "<import/collection>"


def _fspath(value) -> str | None:
    if isinstance(value, int) or value is None:
        return None
    try:
        raw = os.fspath(value)
    except TypeError:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw:
        return None
    if not os.path.isabs(raw):
        raw = os.path.join(os.getcwd(), raw)
    return _norm(raw)


def _is_live_root(path: str) -> bool:
    """True when *path* is real user state the suite has no business writing."""
    if not path.startswith(_REAL_HOME + os.sep):
        return False
    if path == _OUT_PATH:
        return False
    for prefix in _EXCLUDED_PREFIXES:
        if path.startswith(prefix):
            return False
    for part in _EXCLUDED_PARTS:
        if part in path:
            return False
    for tail in _EXCLUDED_TAILS:
        if path.endswith(tail):
            return False
    return True


def _severity(path: str) -> str:
    """Rank a hit. config.yaml and logs/ are the ones that caused real damage."""
    if path.startswith(_VENV_ROOT + os.sep):
        return "venv"
    if not path.startswith(_HERMES_ROOT + os.sep):
        return "home"  # ~/.honcho, ~/.hindsight, ~/.local/bin, ...
    tail = path[len(_HERMES_ROOT) + 1 :]
    if tail.endswith("config.yaml") or tail.endswith(".env"):
        return "critical"
    if tail.startswith("logs" + os.sep) or tail.endswith(".log"):
        return "critical"
    return "hermes"


def _stack() -> list[str]:
    """Frames from the code under test, innermost first, stdlib elided."""
    frames: list[str] = []
    frame = sys._getframe(2)
    while frame is not None and len(frames) < 14:
        name = frame.f_code.co_filename
        low = _norm(name)
        if low != _norm(__file__) and "\\lib\\" not in low and "/lib/" not in low:
            frames.append(f"{name}:{frame.f_lineno} in {frame.f_code.co_name}")
        frame = frame.f_back
    return frames


def _emit(event: str, path: str) -> None:
    key = (current_nodeid, event, path)
    with _lock:
        if key in _seen:
            return
        _seen.add(key)
    record = {
        "nodeid": current_nodeid,
        "event": event,
        "path": path,
        "severity": _severity(path),
        "stack": _stack(),
    }
    _records.append(record)
    try:
        with open(_OUT_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _hook(event: str, args) -> None:
    # Re-entrancy: our own json/open/getcwd calls raise audit events too.
    if getattr(_state, "busy", False):
        return
    try:
        if event == "open":
            # (path, mode, flags). mode is None for os.open.
            mode = args[1]
            if isinstance(mode, str):
                if not ("w" in mode or "a" in mode or "x" in mode or "+" in mode):
                    return
            else:
                flags = args[2]
                if not isinstance(flags, int) or not (flags & _WRITE_FLAGS):
                    return
            indices: tuple[int, ...] = (0,)
        else:
            indices = _PATH_EVENTS.get(event)  # type: ignore[assignment]
            if indices is None:
                return

        _state.busy = True
        try:
            for index in indices:
                if index >= len(args):
                    continue
                path = _fspath(args[index])
                if path is not None and _is_live_root(path):
                    _emit(event, path)
        finally:
            _state.busy = False
    except Exception:  # an audit hook must never break the program
        _state.busy = False


sys.addaudithook(_hook)


# ---------------------------------------------------------------------------
# pytest wiring: attribute each hit to the test that caused it.
# ---------------------------------------------------------------------------


def pytest_runtest_protocol(item, nextitem):
    global current_nodeid
    current_nodeid = item.nodeid
    return None


def pytest_collection(session):
    global current_nodeid
    current_nodeid = "<import/collection>"


def pytest_terminal_summary(terminalreporter):
    global current_nodeid
    current_nodeid = "<teardown/summary>"
    if not _records:
        terminalreporter.write_line("LIVE-ROOT-AUDIT: 0 writes into the real home")
        return
    by_severity: dict[str, int] = {}
    for record in _records:
        by_severity[record["severity"]] = by_severity.get(record["severity"], 0) + 1
    terminalreporter.write_line(
        "LIVE-ROOT-AUDIT: %d writes into the real home (%s) -> %s"
        % (
            len(_records),
            ", ".join(f"{k}={v}" for k, v in sorted(by_severity.items())),
            _OUT_PATH,
        )
    )
    for record in sorted(_records, key=lambda r: (r["severity"] != "critical", r["nodeid"])):
        terminalreporter.write_line(
            "  [%s] %s -> %s(%s)" % (record["severity"], record["nodeid"], record["event"], record["path"])
        )
