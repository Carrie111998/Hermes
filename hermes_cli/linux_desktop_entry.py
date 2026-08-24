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

Import-light and side-effect-free at import time: the uninstaller uses
this without loading the full CLI.
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


def resolve_exec_command(project_root: Optional[Path] = None) -> str:
    """Build the absolute ``Exec=`` command line for ``hermes desktop``.

    Prefer the real ``hermes`` executable (argv[0] or PATH). When Hermes
    runs as a module with no launcher installed, use the current
    interpreter, also absolute.

    The persisted entry must be launch-context independent: whatever
    process writes it, the next launch must read and rewrite the same
    bytes. ``resolve_hermes_bin()`` prefers ``sys.argv[0]``, which differs
    per launch path (wrapper, repo script, ``python -m``), so for this
    one caller an argv[0] that points inside the checkout is not a
    durable installed launcher — skip it and resolve from PATH instead.
    Otherwise a broken entry keeps regenerating itself (the repo-script
    form pins a mutable uv interpreter path; the ``python -m`` form
    persists a bare ``<python> desktop`` that no DE can run).

    ``project_root`` pins which checkout counts as "internal"; defaults to
    the running checkout.
    """
    from hermes_cli.relaunch import resolve_hermes_bin

    bin_path = _resolve_hermes_bin_for_desktop_entry(
        resolve_hermes_bin, checkout_root=project_root
    )
    if bin_path:
        resolved = Path(bin_path).resolve()
        if _needs_interpreter(resolved):
            # The resolved launcher is a Python script whose shebang points at
            # a NON-venv interpreter (e.g. the repo's `hermes` script with
            # `#!/usr/bin/env python3` when argv[0] came from the shell
            # installer's bash wrapper). Launched from the .desktop entry that
            # shebang resolves to the SYSTEM python and dies on the first
            # third-party import (#90292) — silently, since Terminal=false.
            # sys.executable is the interpreter actually running Hermes (the
            # venv one), so prefix it explicitly.
            argv = [str(Path(sys.executable).resolve()), str(resolved), "desktop"]
        else:
            argv = [str(resolved), "desktop"]
    else:
        argv = [str(Path(sys.executable).resolve()), "-m", "hermes_cli.main", "desktop"]
    return " ".join(_quote_exec_arg(a) for a in argv)


def _resolve_hermes_bin_for_desktop_entry(
    resolve_fn=None,
    checkout_root: Optional[Path] = None,
) -> Optional[str]:
    """Resolve the launcher binary for the persisted ``.desktop`` entry.

    Wraps :func:`hermes_cli.relaunch.resolve_hermes_bin` with one
    desktop-entry-specific rule: an ``argv[0]`` that points inside this
    checkout is a launch-context artifact (the repo ``hermes`` script the
    wrapper execs with, or an interpreter binary surfaced by programmatic
    relaunch paths), not a durable installed launcher. Persisting it makes
    the entry a function of however the previous launch happened — the
    bootstrap loop behind #90492's incomplete fix. Skip argv[0]/relative
    candidates in that case and fall through to PATH, where the shell
    installer's wrapper lives.

    ``resolve_fn`` is injectable for tests.
    """
    if resolve_fn is None:
        from hermes_cli.relaunch import resolve_hermes_bin as resolve_fn

    if checkout_root is None:
        checkout_root = _project_root()
    checkout_root = Path(checkout_root).resolve()
    original_argv0 = sys.argv[0]

    def _inside_checkout(candidate: str) -> bool:
        try:
            path = Path(candidate).resolve()
        except OSError:
            return False
        # The repo `hermes` script and anything else shipped in the tree is
        # checkout-internal.
        if path == checkout_root or checkout_root in path.parents:
            return True
        # The `python -m hermes_cli.main` relaunch context surfaces the
        # invoking interpreter (or a non-executable main.py, which the
        # resolver already skips) as argv[0]; an interpreter is never a
        # durable, launchable entry target (it would persist a bare
        # `<python> desktop`). Compare against the *invoking* interpreter
        # (argv[0]'s own file), not sys.executable — under test harnesses
        # they differ.
        try:
            if path.samefile(original_argv0) and _is_interpreter(path):
                return True
        except OSError:
            pass
        return False

    def _is_interpreter(candidate: Path) -> bool:
        """A python interpreter binary (``bin/python*``), not a launcher."""
        name = candidate.name.lower()
        return candidate.parent.name in {"bin", "scripts"} and (
            name == "python" or name.startswith("python")
        )

    # Only reroute when argv[0] actually drove the resolution: re-run the
    # resolver with argv[0] hidden and compare. If PATH yields nothing,
    # keep the resolver's original answer (its fallback chain stays
    # authoritative; #90492 semantics preserved).
    sys.argv[0] = ""
    try:
        rerouted = resolve_fn()
    finally:
        sys.argv[0] = original_argv0

    if rerouted is None:
        # PATH had no `hermes` — common in stripped systemd user sessions
        # and autostart relaunches where ~/.local/bin is absent from PATH.
        # The installer's wrapper lives at a known XDG location; probe it
        # directly before giving up, otherwise we'd silently persist the
        # uv-pinned interpreter form this fix exists to prevent.
        probe = _known_wrapper_candidates()
        for candidate in probe:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)

    primary = resolve_fn()
    if primary and _inside_checkout(primary) and rerouted:
        return rerouted
    if primary and _inside_checkout(primary):
        # argv[0] was checkout-internal AND no durable wrapper exists
        # anywhere (PATH miss, known locations miss). Persisting the
        # interpreter itself would produce an unrunnable `<python>
        # desktop`; dropping to None lets resolve_exec_command emit its
        # runnable `sys.executable -m hermes_cli.main desktop` fallback.
        return None
    return primary


def _known_wrapper_candidates():
    """Durable installed-launcher locations, most likely first.

    Mirrors the installer's ``get_command_link_dir()`` layouts: user
    (``~/.local/bin``), root FHS (``/usr/local/bin``), and Termux
    (``$PREFIX/bin``). The wrapper is always named ``hermes``.
    """
    candidates = []
    home = Path.home()
    prefix = os.environ.get("PREFIX")
    if prefix:
        candidates.append(Path(prefix) / "bin" / "hermes")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        candidates.append(Path("/usr/local/bin/hermes"))
    candidates.append(home / ".local" / "bin" / "hermes")
    return candidates


def _project_root() -> Path:
    """This file lives at ``<checkout>/hermes_cli/linux_desktop_entry.py``."""
    return Path(__file__).resolve().parent.parent


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
    # already resolves correctly; anything else (``/usr/bin/env python3``,
    # a system path) would escape the venv when spawned by the DE.
    exe_dir = str(Path(sys.executable).resolve().parent)
    return exe_dir not in shebang


def _quote_exec_arg(arg: str) -> str:
    """Quote one ``Exec`` argument per the desktop entry spec.

    Reserved characters require double quotes. Inside the quotes, escape
    a backslash and a double quote with a backslash.
    """
    if not any(c in arg for c in " \t\n\"'\\><~|&;$*?#()`"):
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
    contents = render_desktop_entry(resolve_exec_command(project_root), icon_value)

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
