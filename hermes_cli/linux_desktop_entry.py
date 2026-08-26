"""Install and remove the Linux desktop entry (``hermes.desktop``).

``hermes desktop`` builds and launches the Electron app. On Linux, a
freshly-built app has no launcher presence: no menu item, no icon. This
module writes the XDG desktop entry that gives it one.
``hermes uninstall --gui`` removes the entry again.

Two values must be absolute for the entry to work:

  - ``Exec`` — the launcher runs without shell ``PATH`` customizations, so
    a bare ``hermes desktop`` fails when hermes lives in ``~/.local/bin``
    or a venv. Resolve the real binary and write its full path.
  - ``Icon`` — an unqualified icon name needs an indexed icon theme. The
    spec allows an absolute path instead, so point at the app icon in the
    checkout. Do not copy the icon: ``Exec`` already depends on that tree.

Cache refresh is best-effort and tool-gated: ``update-desktop-database``
for the freedesktop menu cache, and ``kbuildsycoca6``/``kbuildsycoca5``
for Plasma. Run each tool only when it exists. A missing tool is not an
error.

Import-light and side-effect-free at import time: the uninstaller and the
Electron main process both use this without loading the full CLI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

DESKTOP_ENTRY_NAME = "hermes.desktop"


def is_supported() -> bool:
    """XDG desktop entries exist only on Linux and BSD."""
    return sys.platform.startswith(("linux", "freebsd", "openbsd", "netbsd"))


def _xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share"


def desktop_entry_path() -> Path:
    """Where the ``hermes.desktop`` entry lives."""
    return _xdg_data_home() / "applications" / DESKTOP_ENTRY_NAME


def icon_path(project_root: Path) -> Path:
    """The app icon shipped in the desktop workspace."""
    return project_root / "apps" / "desktop" / "assets" / "icon.png"


def resolve_exec_command() -> str:
    """Build the absolute ``Exec=`` command line for ``hermes desktop``.

    Prefer the real ``hermes`` executable (argv[0] or PATH). When Hermes
    runs as a module with no launcher installed, use the current
    interpreter, also absolute.
    """
    from hermes_cli.relaunch import resolve_hermes_bin

    bin_path = resolve_hermes_bin()
    if bin_path:
        resolved = Path(bin_path).resolve()
        if _needs_interpreter(resolved):
            # The resolved launcher is a Python script whose shebang points at
            # a NON-venv interpreter (e.g. the repo's `hermes` script with
            # `#!/usr/bin/env python3` when argv[0] came from the shell
            # installer's bash wrapper). Launched from the .desktop entry that
            # shebang resolves to the SYSTEM python and dies on the first
            # third-party import (#90292) — silently, since Terminal=false.
            # ``sys.executable`` (NOT resolved: a venv's ``bin/python`` is a
            # symlink to the base interpreter, and following it discards the
            # venv's site-packages — the very thing the prefix exists to
            # preserve) is the interpreter actually running Hermes. Prefix it
            # only once it is proven to import hermes_cli on its own: it may
            # only see the package through the current working directory (the
            # bootstrap runtime interpreter run from the checkout does), and the
            # DE launches with a cwd of its own choosing, so an unverified
            # prefix trades one silent death for another.
            interpreter = _running_interpreter()
            if _interpreter_imports_hermes_cli(interpreter):
                argv = [interpreter, str(resolved), "desktop"]
            else:
                argv = [str(resolved), "desktop"]
        else:
            argv = [str(resolved), "desktop"]
    else:
        argv = [_running_interpreter(), "-m", "hermes_cli.main", "desktop"]
    return " ".join(_quote_exec_arg(a) for a in argv)


def _running_interpreter() -> str:
    """Absolute path to the interpreter running Hermes, venv symlink intact.

    ``Path.resolve()`` is deliberately avoided: inside a virtual environment
    ``bin/python`` is a symlink to the base interpreter, and invoking the
    resolved target runs WITHOUT the venv's ``site-packages``. Only
    ``os.path.abspath`` is applied, which makes the path absolute without
    following links.
    """
    return os.path.abspath(sys.executable)


def _interpreter_imports_hermes_cli(interpreter: str) -> bool:
    """Whether ``interpreter`` can import ``hermes_cli`` independent of cwd.

    Run the probe from a neutral directory with ``-I`` so neither the current
    working directory nor a user site-packages tree can make an interpreter
    look capable when the desktop entry's own launch context would not.
    """
    try:
        result = subprocess.run(
            [interpreter, "-I", "-c", "import hermes_cli"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.sep,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _needs_interpreter(bin_path: Path) -> bool:
    """Whether ``bin_path`` is a Python script that must run under
    ``sys.executable`` to see Hermes' venv (rather than its own shebang)."""
    try:
        with open(bin_path, "rb") as fh:
            head = fh.readline(256)
    except OSError:
        return False
    if not head.startswith(b"#!"):
        # Native binary (uv tool shim, PyInstaller, distro package) — its own
        # loader is self-sufficient.
        return False
    shebang = head.decode("utf-8", errors="replace").strip().lower()
    if "python" not in shebang:
        # A shell wrapper (e.g. the installer's bash launcher) execs the venv
        # python itself — leave it alone.
        return False
    # A python shebang pointing INSIDE the running interpreter's environment
    # already resolves correctly.
    exe_dir = os.path.dirname(_running_interpreter())
    if exe_dir in shebang:
        return False
    # Otherwise the shebang may still be right and sys.executable wrong: pip
    # writes a console script's shebang to the environment it installed into,
    # while the process writing this entry can be a DIFFERENT interpreter (the
    # bootstrap runtime python) that only reaches hermes_cli through its cwd.
    # An absolute shebang pointing into a real virtual environment is
    # authoritative; only ``/usr/bin/env python3`` and bare system paths — the
    # #90292 case — need the override.
    return not _shebang_targets_virtualenv(shebang)


def _shebang_targets_virtualenv(shebang: str) -> bool:
    """Whether an absolute shebang points at an interpreter inside a venv.

    A virtual environment is identified by ``pyvenv.cfg`` next to the ``bin``
    directory holding the interpreter — the same marker CPython itself uses.
    """
    interpreter = shebang.lstrip("#!").strip().split()[0] if shebang.lstrip("#!").strip() else ""
    if not interpreter.startswith("/") or interpreter.endswith("/env"):
        return False
    interpreter_path = Path(interpreter)
    if not interpreter_path.is_file():
        return False
    return (interpreter_path.parent.parent / "pyvenv.cfg").is_file()


def _quote_exec_arg(arg: str) -> str:
    """Quote one ``Exec`` argument per the desktop entry spec.

    Reserved characters require double quotes. Inside the quotes, escape
    a backslash and a double quote with a backslash.
    """
    if not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_desktop_entry(exec_command: str, icon: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Hermes\n"
        "GenericName=Hermes Desktop\n"
        "Comment=Launch Hermes Desktop\n"
        f"Exec={exec_command}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "StartupNotify=true\n"
        "StartupWMClass=Hermes\n"
    )


def refresh_desktop_databases(applications_dir: Path) -> "list[str]":
    """Reindex the menu caches. Run each tool only when it exists.

    Return the names of the tools that ran (for logging and tests).
    """
    ran: list[str] = []

    update_db = shutil.which("update-desktop-database")
    if update_db:
        if _run_quiet([update_db, str(applications_dir)]):
            ran.append("update-desktop-database")

    # Plasma 6 first, then Plasma 5. Only one of them is ever installed.
    for tool in ("kbuildsycoca6", "kbuildsycoca5"):
        resolved = shutil.which(tool)
        if not resolved:
            continue
        if _run_quiet([resolved, "--noincremental"]):
            ran.append(tool)
        break

    return ran


def _run_quiet(cmd: "list[str]") -> bool:
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def install_desktop_entry(project_root: Path) -> Optional[Path]:
    """Write (or refresh) the Hermes desktop entry. Return its path.

    Return ``None`` on non-Linux platforms or when the write fails. This
    is a convenience, never a reason to fail a launch.
    """
    if not is_supported():
        return None

    entry_path = desktop_entry_path()
    icon = icon_path(project_root)
    # Use the themed name when the checkout has no icon (a lite or
    # packaged install). A broken absolute path renders as no icon.
    icon_value = str(icon) if icon.is_file() else "hermes"
    contents = render_desktop_entry(resolve_exec_command(), icon_value)

    try:
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        # When nothing changed, skip the rewrite. Then a launch does not
        # churn the menu caches.
        if entry_path.is_file() and entry_path.read_text(encoding="utf-8") == contents:
            return entry_path
        entry_path.write_text(contents, encoding="utf-8")
        # Some launchers (and older Plasma) offer the entry only when it
        # is executable.
        entry_path.chmod(0o755)
    except OSError:
        return None

    refresh_desktop_databases(entry_path.parent)
    return entry_path
