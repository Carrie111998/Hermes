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

import json
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


def _desktop_stamp_path(project_root: Path) -> Path:
    """Return the path to the desktop build stamp file under HERMES_HOME."""
    # Import here to avoid circular imports
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "desktop-build-stamp.json"


def _verify_build_stamp_matches(exe_path: Path, project_root: Path) -> bool:
    """Verify the executable belongs to the current project and build generation.
    
    Reads the install-stamp.json next to the binary and compares content_hash
    against the current project's computed hash. Returns False for stale,
    incomplete, or unrelated artifacts (wrong project, wrong profile, corrupted build).
    """
    try:
        # The stamp is at HERMES_HOME/desktop-build-stamp.json
        stamp_file = _desktop_stamp_path(project_root)
        if not stamp_file.is_file():
            return False
        
        stamp_data = json.loads(stamp_file.read_text(encoding="utf-8"))
        expected_hash = stamp_data.get("contentHash")
        if not expected_hash:
            return False
        
        # Compute current project's content hash
        from hermes_cli.main import _compute_desktop_content_hash
        current_hash = _compute_desktop_content_hash(project_root)
        
        return current_hash == expected_hash
    except Exception:
        # Any error (missing stamp, malformed JSON, import error) = no match
        return False


def _read_stamp_content_hash(project_root: Path) -> str:
    """Read contentHash from desktop-build-stamp.json for sorting."""
    try:
        stamp_file = _desktop_stamp_path(project_root)
        if stamp_file.is_file():
            stamp_data = json.loads(stamp_file.read_text(encoding="utf-8"))
            return stamp_data.get("contentHash", "")
    except Exception:
        pass
    return ""


def _find_packaged_candidates(project_root: Path) -> list[Path]:
    """Find all packaged Electron executables, ordered by arch priority and build generation.
    
    Order: linux-unpacked (x86_64) > linux-arm64-unpacked > any other.
    Within same arch, stamp content_hash wins (newer generation = higher hash sort).
    No mtime dependency; stamp hash is the single source of truth.
    """
    release = project_root / "apps" / "desktop" / "release"
    arch_order = ["linux-unpacked", "linux-arm64-unpacked"]
    candidates = []
    for idx, arch in enumerate(arch_order):
        exe = release / arch / "Hermes"
        if exe.exists():
            # Use arch index as primary sort key (lower = higher priority)
            # content_hash as secondary (for same arch, though typically only one per arch)
            content_hash = _read_stamp_content_hash(project_root)
            candidates.append((idx, content_hash, exe))
    # Sort: explicit arch priority first (idx ascending), then stamp hash
    candidates.sort(key=lambda x: (x[0], x[1]))
    return [p for _, _, p in candidates]


def packaged_linux_executable(project_root: Path) -> Optional[Path]:
    """Return a verified, executable packaged Electron binary, or None.
    
    Checks (in order):
    1. File exists and is a regular file
    2. File is executable (os.X_OK)
    3. Build stamp matches current project/generation (prevents stale/wrong-project artifacts)
    """
    for exe in _find_packaged_candidates(project_root):
        if exe.is_file() and os.access(exe, os.X_OK):
            if _verify_build_stamp_matches(exe, project_root):
                return exe
    return None


def resolve_exec_command(project_root: Path) -> str:
    """Build the absolute ``Exec=`` command line for ``hermes desktop``.

    Prefer the real ``hermes`` executable (argv[0] or PATH). When Hermes
    runs as a module with no launcher installed, use the current
    interpreter, also absolute.
    
    CRITICAL FIX: When a packaged Electron app exists, launch it DIRECTLY
    with the required flags (--no-sandbox, --disable-gpu, --ozone-platform=x11)
    instead of going through 'hermes desktop' which uses subprocess.run() and
    blocks waiting for the GUI app to exit — breaking desktop launcher semantics.
    """
    from hermes_cli.relaunch import resolve_hermes_bin

    # First, check if there's a packaged Electron executable we can launch directly
    # This avoids the blocking subprocess.run() in cmd_gui()
    exe = packaged_linux_executable(project_root)
    
    if exe:
        # Launch the Electron binary directly with required flags for Linux/Wayland
        argv = [
            str(exe),
            "--no-sandbox",
            "--disable-gpu",
            "--disable-gpu-process",
            "--in-process-gpu",
            "--ozone-platform=x11",
        ]
        return " ".join(_quote_exec_arg(a) for a in argv)

    # Fallback: use the hermes CLI (for cases where desktop isn't built yet)
    bin_path = resolve_hermes_bin()
    if bin_path:
        resolved = Path(bin_path).resolve()
        if _needs_interpreter(resolved):
            argv = [str(Path(sys.executable).resolve()), str(resolved), "desktop"]
        else:
            argv = [str(resolved), "desktop"]
    else:
        argv = [str(Path(sys.executable).resolve()), "-m", "hermes_cli.main", "desktop"]
    return " ".join(_quote_exec_arg(a) for a in argv)


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
