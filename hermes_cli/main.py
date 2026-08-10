#!/usr/bin/env python3
"""
Hermes CLI - Main entry point.

Usage:
    hermes                     # Interactive chat (default)
    hermes chat                # Interactive chat
    hermes gateway             # Run gateway in foreground
    hermes gateway start       # Start gateway as service
    hermes gateway stop        # Stop gateway service
    hermes gateway status      # Show gateway status
    hermes gateway install     # Install gateway service
    hermes gateway uninstall   # Uninstall gateway service
    hermes setup               # Interactive setup wizard
    hermes logout              # Clear stored authentication
    hermes status              # Show status of all components
    hermes cron                # Manage cron jobs
    hermes cron list           # List cron jobs
    hermes cron status         # Check if cron scheduler is running
    hermes doctor              # Check configuration and dependencies
    hermes honcho setup                    # Configure Honcho AI memory integration
    hermes honcho status                   # Show Honcho config and connection status
    hermes honcho sessions                 # List directory â†’ session name mappings
    hermes honcho map <name>               # Map current directory to a session name
    hermes honcho peer                     # Show peer names and dialectic settings
    hermes honcho peer --user NAME         # Set user peer name
    hermes honcho peer --ai NAME           # Set AI peer name
    hermes honcho peer --reasoning LEVEL   # Set dialectic reasoning level
    hermes honcho mode                     # Show current memory mode
    hermes honcho mode [hybrid|honcho|local]  # Set memory mode
    hermes honcho tokens                   # Show token budget settings
    hermes honcho tokens --context N       # Set session.context() token cap
    hermes honcho tokens --dialectic N     # Set dialectic result char cap
    hermes honcho identity                 # Show AI peer identity representation
    hermes honcho identity <file>          # Seed AI peer identity from a file (SOUL.md etc.)
    hermes honcho migrate                  # Step-by-step migration guide: OpenClaw native â†’ Hermes + Honcho
    hermes version             Show version
    hermes update              Update to latest version
    hermes uninstall           Uninstall Hermes Agent
    hermes acp                 Run as an ACP server for editor integration
    hermes sessions browse     Interactive session picker with search

    hermes claw migrate --dry-run  # Preview migration without changes
"""

# IMPORTANT: hermes_bootstrap must be the very first import â€” it sets up
# UTF-8 stdio on Windows so print()/subprocess children don't hit
# UnicodeEncodeError with non-ASCII characters.  No-op on POSIX.
#
# Guarded against ModuleNotFoundError because ``hermes_bootstrap`` is a
# top-level module registered via pyproject.toml's ``py-modules`` list.
# When the user upgrades code via ``git pull`` (or ``hermes update``
# crashes between ``git reset --hard`` and ``uv pip install -e .``), the
# new code references ``hermes_bootstrap`` but the editable install's
# ``.pth`` file still points at the old set of top-level modules.  Without
# this guard, hermes crashes on import and the user can't run
# ``hermes update`` to recover.  Missing the bootstrap means UTF-8 stdio
# setup is skipped on Windows â€” degraded, not broken.  POSIX is unaffected.
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    pass

# Windows: neutralize CPython's ``platform._syscmd_ver`` before anything else
# imports â€” it shells out ``cmd /c ver`` (shell=True, no CREATE_NO_WINDOW), so
# any dependency touching ``platform.uname()`` at import time flashes a
# visible console when this process is windowless (pythonw gateway + every
# kanban worker).  No-op on POSIX; never raises.
from hermes_cli._subprocess_compat import suppress_platform_ver_console

suppress_platform_ver_console()

import os
import sys

# â”€â”€ Startup fast-path bootstrap â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Two lines of inline path math so ``python hermes_cli/main.py`` (script
# mode â€” sys.path[0] is hermes_cli/, not the repo root) can import the
# canonical helpers; everything else lives in hermes_cli._startup_fast.
_bootstrap_root = os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir))
if _bootstrap_root not in sys.path:
    sys.path.insert(0, _bootstrap_root)
from hermes_cli import _startup_fast  # noqa: E402

# Early venv self-heal â€” MUST run before any third-party import below.  When
# a prior ``hermes update`` left a recovery marker and a core package's import
# files were wiped (#57828 â€” failed lazy backend refresh), the module-level
# ``from hermes_cli.env_loader import ...`` / ``from hermes_cli.config import
# ...`` imports further down would crash before ``main()`` ever reaches
# ``_recover_from_interrupted_install()``.  ``_early_recovery`` is stdlib-only
# (safe to import on a corrupted venv), repairs just enough for this module to
# finish importing, and leaves the marker lifecycle to the full recovery path.
# The module import itself is unguarded on purpose: it lives in this same
# package directory, so if IT can't import, nothing else in hermes_cli can
# either. It is also the canonical home of the probe/repair tables reused by
# the full recovery path below.
from hermes_cli import _early_recovery as _early_recovery_mod

try:
    _early_recovery_mod.recover_if_needed()
except Exception:
    pass


def _exit_after_oneshot(rc: object) -> None:
    """Exit one-shot mode without letting late native finalizers change rc.

    The SIGABRT this guards against (#30387, #43055) fires in a
    native-extension finalizer during CPython's ``Py_FinalizeEx``, *after*
    the response has printed. Flush streams, shut down file logging, then
    ``os._exit`` past interpreter finalization. The ``atexit`` chain is
    deliberately skipped â€” several handlers re-enter native code that may
    be the abort source. Stateful cleanup is handled in ``_run_agent`` and
    ``_cleanup_oneshot_runtime``.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    try:
        logging.shutdown()
    except Exception:
        pass
    if rc is None:
        exit_code = 0
    elif isinstance(rc, int):
        exit_code = rc
    else:
        exit_code = 1
    os._exit(exit_code)


_oneshot_cleanup_done = False


def _cleanup_oneshot_runtime() -> None:
    """Best-effort process-global cleanup before one-shot hard exit.

    ``run_oneshot`` owns the agent-local cleanup (memory provider, agent.close,
    session_db.close â€” all in ``_run_agent``'s finally block). This mirrors the
    process-global pieces from ``cli.py:_run_cleanup()`` that would otherwise
    be skipped by ``os._exit``.
    """
    global _oneshot_cleanup_done
    if _oneshot_cleanup_done:
        return
    _oneshot_cleanup_done = True
    try:
        from tools.terminal_tool import cleanup_all_environments
        cleanup_all_environments()
    except Exception:
        pass
    try:
        from tools.async_delegation import interrupt_all
        interrupt_all(reason="oneshot shutdown")
    except Exception:
        pass
    try:
        from tools.browser_tool import _emergency_cleanup_all_sessions
        _emergency_cleanup_all_sessions()
    except Exception:
        pass
    try:
        from tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
    except BaseException:
        pass
    try:
        from agent.auxiliary_client import shutdown_cached_clients
        shutdown_cached_clients()
    except Exception:
        pass


def _run_and_exit_oneshot(
    prompt: str,
    *,
    model: object = None,
    provider: object = None,
    toolsets: object = None,
    usage_file: object = None,
) -> None:
    try:
        from hermes_cli.oneshot import run_oneshot

        rc = run_oneshot(
            prompt,
            model=model,
            provider=provider,
            toolsets=toolsets,
            usage_file=usage_file,
        )
    except KeyboardInterrupt:
        rc = 130
    except SystemExit as exc:
        if exc.code is not None and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
            rc = 1
        else:
            rc = exc.code
    except BaseException:
        # Defense-in-depth. ``run_oneshot`` already converts agent failures
        # into an int return code and only re-raises KeyboardInterrupt /
        # SystemExit (handled above). Anything still escaping here means
        # ``run_oneshot`` itself malfunctioned â€” surface it on stderr but never
        # fall through to normal interpreter teardown, which is the exact path
        # that aborts with SIGABRT on AL2023 (the bug this routine fixes).
        import traceback
        try:
            traceback.print_exc()
        except Exception:
            pass
        rc = 1
    try:
        _cleanup_oneshot_runtime()
    finally:
        # The hard exit is the safety boundary for #43055. Even an interrupt
        # during best-effort cleanup must not fall back into interpreter
        # finalization, where the reported native SIGABRT occurs.
        _exit_after_oneshot(rc)


def _project_root_str_fast() -> str:
    return _startup_fast.project_root_str()


def _ensure_project_root_on_path_fast() -> None:
    _startup_fast.ensure_project_root_on_path()


def _set_process_title() -> None:
    """Set the process title to 'hermes' so tools like 'ps', 'top', and
    'htop' show the app name instead of 'python3.xx'.

    Purely cosmetic â€” non-fatal on any platform.

    Strategy (try in order):
      1. ``setproctitle`` (opt-in dep â€” installed via ``hermes tools`` or
         ``pip install setproctitle``, or bundled in a future release).
      2. ctypes ``prctl(PR_SET_NAME)`` (Linux only, 15-char limit).
      3. ctypes ``pthread_setname_np`` (macOS only, kernel thread name â€”
         changes lldb/top but not ``ps aux``).
      4. No-op on Windows (the .exe name is already ``hermes.exe``).
    """
    # Strategy 1: setproctitle (best â€” works on macOS, Linux, BSD)
    try:
        import setproctitle  # type: ignore[import-untyped]

        setproctitle.setproctitle("hermes")
        return
    except ImportError:
        pass

    # Strategy 2/3: platform-specific ctypes fallback
    import ctypes
    import platform

    try:
        system = platform.system()
        if system == "Linux":
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(15, b"hermes", 0, 0, 0)  # PR_SET_NAME = 15
        elif system == "Darwin":
            libc = ctypes.CDLL("libc.dylib", use_errno=True)
            libc.pthread_setname_np(b"hermes")
        # Windows: the .exe name is already ``hermes.exe`` â€” nothing to do.
    except Exception:
        pass


# Cheap, dependency-free read of `display.interface` from config.yaml for the
# earliest hot-path decisions (mouse-residue suppression, Termux fast launch)
# that run *before* hermes_cli.config is importable. Mirrors the explicit
# precedence used everywhere else: `--cli` always wins, then `--tui`/env, then
# this config value. Cached so the multiple early callers don't re-parse YAML.
_EARLY_INTERFACE_CACHE: "list | None" = None


def _config_default_interface_early() -> str:
    """Return the configured default interface ("cli"/"tui") via a minimal
    YAML read. Best-effort: any error falls back to "cli" (legacy behavior)."""
    global _EARLY_INTERFACE_CACHE
    if _EARLY_INTERFACE_CACHE is not None:
        return _EARLY_INTERFACE_CACHE[0]
    value = "cli"
    try:
        home = os.environ.get("HERMES_HOME")
        if home:
            cfg_path = os.path.join(home, "config.yaml")
        else:
            cfg_path = os.path.join(os.path.expanduser("~"), ".hermes", "config.yaml")
        if os.path.exists(cfg_path):
            import yaml as _yaml_iface

            with open(cfg_path, encoding="utf-8") as _f:
                raw = _yaml_iface.load(
                    _f, Loader=getattr(_yaml_iface, "CSafeLoader", None) or _yaml_iface.SafeLoader
                ) or {}
            disp = raw.get("display", {})
            if isinstance(disp, dict):
                iface = disp.get("interface")
                if isinstance(iface, str) and iface.strip().lower() == "tui":
                    value = "tui"
    except Exception:
        value = "cli"  # best-effort â€” default to classic REPL on any error
    _EARLY_INTERFACE_CACHE = [value]
    return value


def _wants_tui_early(argv: "list[str] | None" = None) -> bool:
    """Earliest TUI decision, usable before argparse/config imports.

    Precedence: explicit ``--cli`` wins (forces classic REPL), then
    explicit ``--tui``/``HERMES_TUI=1``, then a real-TTY gate (a
    non-interactive stdio can't host the Ink UI, so ambient config never
    boots it there), then ``display.interface`` in config.

    The TTY gate is load-bearing for headless spawners â€” kanban workers,
    cron jobs, pipes run ``hermes â€¦ chat -q`` with stdio on a pipe. This
    is the earliest launch decision (it runs before ``cmd_chat`` /
    ``_resolve_use_tui``), so a ``display.interface: tui`` default used to
    boot the TUI here â€” whose no-TTY bail-out exits 0 without doing the
    task â†’ "protocol violation" on every attempt. An explicit ``--tui``
    still reaches the informative bail-out.
    """
    if argv is None:
        argv = sys.argv[1:]
    if "--cli" in argv:
        return False
    if os.environ.get("HERMES_TUI") == "1" or "--tui" in argv:
        return True
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except Exception:
        return False
    return _config_default_interface_early() == "tui"


# Mouse-tracking residue suppression â€” runs BEFORE every other import on the
# TUI hot path so the terminal stops emitting SGR/X10 mouse reports while the
# Python launcher is still doing imports (â‰ˆ100â€“300ms in cooked + echo mode,
# before the Node TUI takes stdin into raw mode). During that window any
# incoming bytes are echoed straight back to the user's shell scrollback as
# ``^[[<â€¦M`` text. The TUI itself runs `resetTerminalModes()` again in
# `entry.tsx`; this is just the earlier cousin. ``HERMES_TUI_NO_EARLY_DISABLE``
# escapes the behaviour for diagnostics.
def _suppress_mouse_residue_early() -> None:
    if os.environ.get("HERME×®¸ã{h‘éì¶»§q«^u
Bˆ‹K[X^YØ\\ÙXÛÛ™È‹Bˆ\OY›Ø]BˆY˜][S›Û™KBˆ[JBˆ•Ú[™ÝÈ™]ÙY[ˆHÙ^YY™YXÙ\ÜÛÜ‰ÜÈ\ÝXÝ]š]H[™[ˆƒBˆ›Üœ[‰ÜÈÝ\›Üˆ[HÈÛÝ[\ÈHØ[YHÛÛ™\œØ][ÛˆƒBˆŠY˜][ˆL
HƒBˆ
KBˆ
CBƒBˆÙ\ÜÚ[Ûœ×Ü™XÛÝ™\ˆHÙ\ÜÚ[Ûœ×ÜÝXœ\œÙ\œË˜YÜ\œÙ\ŠBˆœ™XÛÝ™\ˆ‹Bˆ[H”™XZ[Ø[›ÛšXØ[Ù\ÜÚ[Ûˆ]H[ÈHÙ\\˜]HÛX[ˆ]X˜\ÙH‹Bˆ\ØÜš\[ÛJBˆ“Ù™›[™K›Û‹Y\ÝXÝ]™H™XÛÝ™\žH›ÜˆH[XYÙYÝ]K™‹ˆHƒBˆœÛÝ\˜ÙH]X˜\ÙH[™]ÈÐSÔÒKÜ›Û˜XÚËZ›Ý\›˜[ÚYXØ\œÈ\™HƒBˆ˜ÛÜYY™Y›Ü™HÔS]HÜ[œÈ[ž][™ËˆØ[›ÛšXØ[›ÝÜÈ\™H™XZ[ƒBˆš[ÈH™]ÈÝ]]]X˜\ÙNÈ\š]™YÙX\˜Ú[™^\È\™H™XÜ™X]YƒBˆ˜[™HXÝ]™H]X˜\ÙH\È™]™\ˆ™\XÙY]]ÛX]XØ[KˆƒBˆ
KBˆ
CBˆÙ\ÜÚ[Ûœ×Ü™XÛÝ™\‹˜YØ\™Ý[Y[
Bˆ‹K\ÛÝ\˜ÙH‹Bˆ\OT]Bˆ™\]Z\™YUYKBˆ[H”ÛÝ\˜ÙHÝ]K™ˆÜˆ™\Ù\™Y˜XÚÝ\È[œÜXÝÜ™XÛÝ™\ˆ‹Bˆ
CBˆÙ\ÜÚ[Ûœ×Ü™XÛÝ™\‹˜YØ\™Ý[Y[
Bˆ‹K[Ý]]‹Bˆ\OT]Bˆ[H“™]È™XÛÝ™\žH]X˜\ÙH]
™\]Z\™Y[›\ÜÈKZ[œÜXÝ[Û›JH‹Bˆ
CBˆÙ\ÜÚ[Ûœ×Ü™XÛÝ™\‹˜YØ\™Ý[Y[
Bˆ‹KZ[œÜXÝ[Û›H‹BˆXÝ[ÛHœÝÜ™WÝYH‹Bˆ[H“Û›H™\ÜØ[›ÛšXØ[X›H™XYXš[]NÈÈ›ÝÜ™X]H[ˆÝ]]]X˜\ÙH‹Bˆ
CBˆÙ\ÜÚ[Ûœ×Ü™XÛÝ™\‹˜YØ\™Ý[Y[
Bˆ‹K]ÛÜšËY\ˆ‹Bˆ\OT]Bˆ[H‘^\Ý[™È\™XÝÜžH›ÜˆH\ÜÜØX›HÛÝ\˜ÙHÛÜH
Y˜][È™\ÚYHHÝ]]
H‹Bˆ
CBˆÙ\ÜÚ[Ûœ×Ü™XÛÝ™\‹˜YØ\™Ý[Y[
Bˆ‹KXÚ[šË\Ú^™H‹Bˆ\OZ[BˆY˜][LLBˆ[H”›ÝÜÈÛÛ[Z]Y\ˆ™XÛÝ™\žH˜]Ú
Y˜][ˆL
H‹Bˆ
CBˆÙ\ÜÚ[Ûœ×Ü™XÛÝ™\‹˜YØ\™Ý[Y[
Bˆ‹KX[ÝË\\X[‹BˆXÝ[ÛHœÝÜ™WÝYH‹Bˆ[JBˆ™\ÝYY™›ÜØ[˜YÙHXÜ›ÜÜÈ[XYÙY›ÝÈ˜[™Ù\ÎÈHÝ]]™[XZ[œÈƒBˆœÙ\\˜]H[™]™\žHÚÚ\Y˜[™ÙH\È™XÛÜ™YƒBˆ
KBˆ
CBˆÙ\ÜÚ[Ûœ×Ü™XÛÝ™\‹˜YØ\™Ý[Y[
Bˆ‹K\™\Ü‹Bˆ\OT]Bˆ[H’”ÓÓˆ™\Ü]
Y˜][ÈÈÝ]]‹œ™XÛÝ™\žKšœÛÛŠH‹Bˆ
CBƒBˆÙ\ÜÚ[Ûœ×ÜÝXœ\œÙ\œË˜YÜ\œÙ\ŠœÝ]È‹[H”ÚÝÈÙ\ÜÚ[ÛˆÝÜ™HÝ]\ÝXÜÈŠCBƒBˆÙ\ÜÚ[Ûœ×Ü™[˜[YHHÙ\ÜÚ[Ûœ×ÜÝXœ\œÙ\œË˜YÜ\œÙ\ŠBˆœ™[˜[YH‹[H”Ù]ÜˆÚ[™ÙHHÙ\ÜÚ[Û‰ÜÈ]HƒBˆ
CBˆÙ\ÜÚ[Ûœ×Ü™[˜[YK˜YØ\™Ý[Y[
œÙ\ÜÚ[Û—ÚY‹[H”Ù\ÜÚ[ÛˆQÈ™[˜[YHŠCBˆÙ\ÜÚ[Ûœ×Ü™[˜[YK˜YØ\™Ý[Y[
]H‹˜\™ÜÏHŠÈ‹[H“™]È]H›ÜˆHÙ\ÜÚ[ÛˆŠCBƒBˆÙ\ÜÚ[Ûœ×Ü™]]HHÙ\ÜÚ[Ûœ×ÜÝXœ\œÙ\œË˜YÜ\œÙ\ŠBˆœ™]]K\ÚÚ[È‹Bˆ[H”™K]]HÙ\ÜÚ[ÛœÈÚÜÙH]]Ë]]HØ[YHœ›ÛHHÜÚÚ[	ÜÈÝÛˆ^‹Bˆ\ØÜš\[ÛJBˆ”Ù\ÜÚ[ÛœÈÜ[™YÚ]HÜÚÚ[Ù\™H]]Ë]]Yœ›ÛHH^[™YƒBˆ›Y\ÜØYÙKÚXÚ[X™YÈHÚÛHÚÚ[›ÙH8 %ÛÈH]HƒBˆ™\ØÜšX™\ÈHÒÒS›ÝH™\]Y\Ýˆ\È™YÙ[™\˜]\ÈÜÙHƒBˆ]\Èœ›ÛHÚ]H\Ù\ˆXÝX[H\Yˆ\ÝÈÚ]]ÛÝ[ƒBˆ˜Ú[™ÙH[›\ÜÈKX\H\È\ÜÙYˆƒBˆ
KBˆ
CBˆÙ\ÜÚ[Ûœ×Ü™]]K˜YØ\™Ý[Y[
Bˆ‹KX\H‹BˆXÝ[ÛHœÝÜ™WÝYH‹Bˆ[H•Üš]HH™]È]\È
Y˜][ˆžH[ŠH‹Bˆ
CBˆÙ\ÜÚ[Ûœ×Ü™]]K˜YØ\™Ý[Y[
Bˆ‹K[[Z]‹Bˆ\OZ[BˆY˜][LŒBˆ[H“X^[][HÙ\ÜÚ[ÛœÈÈ^[Z[™H
Y˜][ˆŒ
H‹Bˆ
CBƒBˆÙ\ÜÚ[Ûœ×Øœ›ÝÜÙHHÙ\ÜÚ[Ûœ×ÜÝXœ\œÙ\œË˜YÜ\œÙ\ŠBˆ˜œ›ÝÜÙH‹Bˆ[H’[\˜XÝ]™HÙ\ÜÚ[ÛˆXÚÙ\ˆ8 %œ›ÝÜÙKÙX\˜Ú[™™\Ý[YHÙ\ÜÚ[ÛœÈ‹Bˆ
CBˆÙ\ÜÚ[Ûœ×Øœ›ÝÜÙK˜YØ\™Ý[Y[
Bˆ‹K\ÛÝ\˜ÙH‹[H‘š[\ˆžHÛÝ\˜ÙH
ÛK[YÜ˜[K\ØÛÜ™]ËŠHƒBˆ
CBˆÙ\ÜÚ[Ûœ×Øœ›ÝÜÙK˜YØ\™Ý[Y[
Bˆ‹K[[Z]‹\OZ[Y˜][ML[H“X^Ù\ÜÚ[ÛœÈÈØY
Y˜][ˆL
HƒBˆ
CBƒBƒBˆÈÛYÜÙ\ÜÚ[ÛœÈ]™\È[ˆ\›Y\×ØÛKÜÙ\ÜÚ[Ûœ×ØÛYœH
XZ[‹œHXÛÛ\ÜÚ][ÛŠKƒBˆÈÙ\ÜÚ[Ûœ×Ü\œÙ\ˆ\È™XYY[ˆšXH[˜ÝÛÛËœ\X[™XØ]\ÙHCBˆÈ˜[›ÝYÚœ˜[˜ÚØ[ÈÙ\ÜÚ[Ûœ×Ü\œÙ\‹œš[Ú[

H
›Ü›Y\›HCBˆÈÛÜÝ\™HØ\\™HÙˆ\ÈXZ[Š
K[ØØ[
KƒBˆÙ\ÜÚ[Ûœ×Ü\œÙ\‹œÙ]ÙY˜][ÊBˆ[˜ÏWÙ[˜ÝÛÛËœ\X[
ÛYÜÙ\ÜÚ[ÛœËÙ\ÜÚ[Ûœ×Ü\œÙ\\Ù\ÜÚ[Ûœ×Ü\œÙ\ŠCBˆ
CBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈ[œÚYÚÈÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËÚ[œÚYÚËœJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[Ú[œÚYÚ×Ü\œÙ\ŠÝXœ\œÙ\œËÛYÚ[œÚYÚÏXÛYÚ[œÚYÚÊCBˆZ[Û[Ûš]Üš[™×Ü\œÙ\ŠÝXœ\œÙ\œËÛYÛ[Ûš]Üš[™ÏXÛYÛ[Ûš]Üš[™ÊCBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈÛ]ÈÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËØÛ]ËœJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[ØÛ]×Ü\œÙ\ŠÝXœ\œÙ\œËÛYØÛ]ÏXÛYØÛ]ÊCBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈ™\œÚ[ÛˆÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËÝ™\œÚ[Û‹œJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[Ý™\œÚ[Û—Ü\œÙ\ŠÝXœ\œÙ\œËÛYÝ™\œÚ[ÛXÛYÝ™\œÚ[ÛŠCBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈ\]HÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËÝ\]KœJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[Ý\]WÜ\œÙ\ŠÝXœ\œÙ\œËÛYÝ\]OXÛYÝ\]JCBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈ[š[œÝ[ÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËÝ[š[œÝ[œJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[Ý[š[œÝ[Ü\œÙ\ŠÝXœ\œÙ\œËÛYÝ[š[œÝ[XÛYÝ[š[œÝ[
CBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈXÜÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËØXÜœJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[ØXÜÜ\œÙ\ŠÝXœ\œÙ\œËÛYØXÜXÛYØXÜ
CBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈ›Ùš[HÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËÜ›Ùš[KœJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[Ü›Ùš[WÜ\œÙ\ŠÝXœ\œÙ\œËÛYÜ›Ùš[OXÛYÜ›Ùš[JCBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈÛÛ\][ÛˆÛÛ[X[™BˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÛÛ\][Û—Ü\œÙ\ˆHÝXœ\œÙ\œË˜YÜ\œÙ\ŠBˆ˜ÛÛ\][Ûˆ‹Bˆ[H”š[Ú[ÛÛ\][ÛˆØÜš\
˜\ÚœÚÜˆš\Ú
H‹Bˆ
CBˆÛÛ\][Û—Ü\œÙ\‹˜YØ\™Ý[Y[
BˆœÚ[‹Bˆ˜\™ÜÏHÈ‹BˆY˜][H˜˜\Ú‹BˆÚÚXÙ\ÏVÈ˜˜\Ú‹žœÚ‹™š\Ú—KBˆ[H”Ú[\H
Y˜][ˆ˜\Ú
H‹Bˆ
CBˆÛÛ\][Û—Ü\œÙ\‹œÙ]ÙY˜][Ê[˜Ï[[X™H\™ÜÎˆÛYØÛÛ\][ÛŠ\™ÜË\œÙ\ŠJCBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈ\Ú›Ø\™ÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËÙ\Ú›Ø\™œJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[Ù\Ú›Ø\™Ü\œÙ\ŠBˆÝXœ\œÙ\œËBˆÛYÙ\Ú›Ø\™XÛYÙ\Ú›Ø\™BˆÛYÙ\Ú›Ø\™Ü™YÚ\Ý\XÛYÙ\Ú›Ø\™Ü™YÚ\Ý\‹Bˆ
CBƒBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈ\ÚÝÜ
KšË˜KˆÝZJHÛÛ[X[™BˆÃBˆÈHØ[›ÛšXØ[˜[YH\È™\ÚÝÜŽÈ™ÝZHˆ\ÈÙ\\ÈH\™XØ]Y[X\ÃBˆÈ›ÜˆÛ™H™[X\ÙKˆH\›Y\ËTÙ]\™^HÝXØÙ\ÜÈØÜ™Y[ˆ[È\Ù\œÈÃBˆÈ[ˆ\›Y\È\ÚÝÜœ›ÛHH\›Z[˜[ÛÈHØ[›ÛšXØ[˜[YH™YYÃBˆÈÈ™HHÛ™H]\X\œÈ[ˆKZ[
\™Ü\œÙH›Û[Ý\ÈHš[X\žCBˆÈ˜[YNÈ[X\Ù\ÈÝ^HY[ŠKƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈÝZHÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËÙÝZKœJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[ÙÝZWÜ\œÙ\ŠÝXœ\œÙ\œËÛYÙÝZOXÛYÙÝZJCBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈÙÜÈÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËÛÙÜËœJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[ÛÙÜ×Ü\œÙ\ŠÝXœ\œÙ\œËÛYÛÙÜÏXÛYÛÙÜÊCBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈ›Û\\Ú^™HÛÛ[X[™
\œÙ\ˆZ[[ˆ\›Y\×ØÛKÜÝX˜ÛÛ[X[™ËÜ›Û\ÜÚ^™KœJCBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆZ[Ü›Û\ÜÚ^™WÜ\œÙ\ŠÝXœ\œÙ\œËÛYÜ›Û\ÜÚ^™OXÛYÜ›Û\ÜÚ^™JCBƒBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈ\œÙH[™^XÝ]CBˆÈOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOCBˆÈ™K\›ØÙ\ÜÈ\™ÝˆÛÈ[œ][ÝY][K]ÛÜ™Ù\ÜÚ[Ûˆ˜[Y\ÈY\ˆXÈÈ\ƒBˆÈ\™HY\™ÙY[ÈHÚ[™ÛHÚÙ[ˆ™Y›Ü™H\™Ü\œÙHÙY\È[KƒBˆÈK™Ëˆ\›Y\ÈXÈÚÙ[[ÛˆYÙ[]˜8¡¤ˆ\›Y\ÈXÈ	ÔÚÙ[[ÛˆYÙ[]‰ØBˆÈ8¥ 8¥ ÛÛZ[™\‹X]Ø\™H›Ý][™È8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ BˆÈÚ[ˆš^ÔÈÛÛZ[™\ˆ[ÙH\ÈXÝ]™K›Ý]HSÝX˜ÛÛ[X[™È[ÃBˆÈHX[˜YÙYÛÛZ[™\‹ˆ\ÈUTÕ[ˆ™Y›Ü™H\œÙWØ\™ÜÊ
HÛÈ]BˆÈKZ[[œ™XÛÙÛš\ÙY›YÜË[™]™\žHÝX˜ÛÛ[X[™\™H›ÜØ\™YBˆÈ˜[œÜ\™[H[œÝXYÙˆ™Z[™È[\˜Ù\YžH\™Ü\œÙHÛˆHÜÝƒBˆœ›ÛH\›Y\×ØÛK˜ÛÛ™šYÈ[\ÜÙ]ØÛÛZ[™\—Ù^X×Ú[™›ÃBƒBˆÛÛZ[™\—Ú[™›ÈHÙ]ØÛÛZ[™\—Ù^X×Ú[™›Ê
CBˆYˆÛÛZ[™\—Ú[™›ÎƒBˆÙ^X×Ú[—ØÛÛZ[™\ŠÛÛZ[™\—Ú[™›ËÞ\Ë˜\™Ý–ÌN—JCBˆÈ[œ™XXÚX›NˆÜË™^XÝœ™]™\ˆ™]\›œÈÛˆÝXØÙ\ÜÈ
›ØÙ\ÜÈ\È™\XÙY
CBˆÈ[™˜Z\Ù\ÈÔÑ\œ›ÜˆÛˆ˜Z[\™H
ÚXÚ›ÜYØ]\È\ÈH˜XÙX˜XÚÊKƒBˆÞ\Ë™^]
JCBƒBˆÜ›ØÙ\ÜÙYØ\™ÝˆHØÛØ[\ØÙWÜÙ\ÜÚ[Û—Û˜[YWØ\™ÜÊÞ\Ë˜\™Ý–ÌN—JCBƒBˆÈ8¥ 8¥ Y™[œÚ]™HÝXœ\œÙ\ˆ›Ý][™È
œËNLÌÎÛÜšØ\›Ý[™
H8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ BˆÈÛˆÛÛYH]Ûˆ™\œÚ[ÛœÈ
›ÝX›HËŒLJK\™Ü\œÙH˜Z[ÈÈ›Ý]CBˆÈÝX˜ÛÛ[X[™ÚÙ[œÈÚ[ˆH\™[\œÙ\ˆ\È˜\™ÜÏIÏÉÈÜ[Û˜[BˆÈ\™Ý[Y[È
KXÛÛ[YJKˆHÞ[\ÛNˆ[œ™XÛÙÛš^™Y\™Ý[Y[Îˆ[Ù[ƒBˆÈ]™[ˆÝYÚ	Û[Ù[	È\ÈH™YÚ\Ý\™YÝX˜ÛÛ[X[™ƒBˆÃBˆÈš^ˆÚ[ˆ\™ÝˆÛÛZ[œÈHÚÙ[ˆX]Ú[™ÈHÛ›ÝÛˆÝX˜ÛÛ[X[™Ù]BˆÈÝXœ\œÙ\œËœ™\]Z\™YUYHÈ›Ü˜ÙH]\›Z[š\ÝXÈ›Ý][™ËˆYˆ]BˆÈ˜Z[È
K™Ëˆ	Ú\›Y\ÈXÈ[Ù[	ÈÚ\™H	Û[Ù[	È\ÈÛÛœÝ[YY\ÈCBˆÈÙ\ÜÚ[Ûˆ˜[YH›ÜˆKXÛÛ[YJK˜[˜XÚÈÈHY˜][™Z]š[Ý\‹ƒBˆ[\Ü[È\ÈÚ[ÃBƒBˆÚÛ›ÝÛ—ØÛYÈH
BˆÙ]
ÝXœ\œÙ\œË˜ÚÚXÙ\ËšÙ^\Ê
JHYˆ\Ø]ŠÝXœ\œÙ\œË˜ÚÚXÙ\ÈŠH[ÙHÙ]

CBˆ
CBˆÚ\×ØÛYÝÚÙ[ˆH[žJBˆ[ˆÚÛ›ÝÛ—ØÛYÈ›Üˆ[ˆÜ›ØÙ\ÜÙYØ\™ÝˆYˆ›ÝœÝ\ÝÚ]
‹HŠCBˆ
CBƒBˆYˆÚ\×ØÛYÝÚÙ[ŽƒBˆÝXœ\œÙ\œËœ™\]Z\™YHYCBˆÜØ]™YÜÝ\œˆHÞ\ËœÝ\œƒBˆžNƒBˆÞ\ËœÝ\œˆHÚ[Ë”Ýš[™ÒSÊ
CBˆ\™ÜÈH\œÙ\‹œ\œÙWØ\™ÜÊÜ›ØÙ\ÜÙYØ\™ÝŠCBˆÞ\ËœÝ\œˆHÜØ]™YÜÝ\œƒBˆ^Ù\Þ\Ý[Q^]\È^ÎƒBˆÞ\ËœÝ\œˆHÜØ]™YÜÝ\œƒBˆÈ[Ý™\œÚ[Ûˆ›YÜÈ
^]ÛÙH
H[™XYHš[YÝ]]8 %BˆÈ™K\˜Z\ÙH[[YYX][HÈ]›ÚYHÙXÛÛ™\œÙWØ\™ÜÈš[[™ÃBˆÈHØ[YH[^YØZ[ˆ
ÌLŒÌ
KƒBˆYˆ^Ë˜ÛÙHOHƒBˆ˜Z\ÙCBˆÈÝX˜ÛÛ[X[™˜[YHØ\ÈÛÛœÝ[YY\ÈH›YÈ˜[YH
K™ËˆXÈ[Ù[
KƒBˆÈ˜[˜XÚÈÈÜ[Û˜[ÝXœ\œÙ\œÈÛÈ\™Ü\œÙH[™\È]›Ü›X[KƒBˆÝXœ\œÙ\œËœ™\]Z\™YH˜[ÙCBˆ\™ÜÈH\œÙ\‹œ\œÙWØ\™ÜÊÜ›ØÙ\ÜÙYØ\™ÝŠCBˆ[ÙNƒBˆÝXœ\œÙ\œËœ™\]Z\™YH˜[ÙCBˆ\™ÜÈH\œÙ\‹œ\œÙWØ\™ÜÊÜ›ØÙ\ÜÙYØ\™ÝŠCBƒBˆÈ[™HK]™\œÚ[Ûˆ›YÃBˆYˆ\™ÜË™\œÚ[ÛŽƒBˆÛYÝ™\œÚ[ÛŠ\™ÜÊCBˆ™]\›ƒBƒBˆÈK^[ÛÎˆÙ]T“QT×ÖSÓ×ÓSÑH
˜™Y›Ü™JˆYÚ[ˆ\ØÛÝ™\žKˆHØ[ÃBˆÈÜ™\\™WØYÙ[ÜÝ\\

H™[ÝÈšYÙÙ\œÈ\ØÛÝ™\—ÜYÚ[œÊ
H8¡¤ˆÛÛBˆÈ[\ÜË[™ÛÛË˜\›Ý˜[œ™Y^™\ÈÖSÓ×ÓSÑWÑ”“Ö‘Sˆ][Ù[CBˆÈ[\Ü[YH
ˆÍÎNMÙXÝ\š]H\™[š[™ÈYØZ[œÝ›Û\Z[š™XÝ[ÛŠKƒBˆÈYˆH[ˆ˜\ˆ\ÈÙ]Û›H]\ˆ
K™Ëˆ[œÚYHÛYØÚ]
KHœ›Þ™[ƒBˆÈ˜[YH\È[™XYH˜[ÙH[™K^[ÛÈÚ[[HÙ\È›Ý[™ËƒBˆYˆÙ]]Š\™ÜËž[ÛÈ‹˜[ÙJNƒBˆÜË™[š\›Û–È’T“QT×ÖSÓ×ÓSÑH—HHŒHƒBƒBˆÈ\ØÛÝ™\ˆ]ÛˆYÚ[œÈ[™™YÚ\Ý\ˆÚ[ÛÚÜÈÛ˜ÙK™Y›Ü™H[žCBˆÈÛÛ[X[™]Ø[ˆš\™HY™XÞXÛHÛÚÜËˆ›Ý\™HY[\Ý[ÈØ]YBˆÈÛÈ[›ÜÜXÝ[Û‹ÛX[˜YÙ[Y[ÛÛ[X[™È
\›Y\ÈÛÚÜÈ\ÝÜ›ÛƒBˆÈ\ÝØ]]Ø^HÝ]\ËXÜY‹‹ŠHÛ‰Ý^H\ØÛÝ™\žHÛÜÝÜƒBˆÈšYÙÙ\ˆÛÛœÙ[›Û\È›ÜˆÛÚÜÈH\Ù\ˆ\ÈÝ[[œÜXÝ[™ËƒBˆÜ™\\™WØYÙ[ÜÝ\\
\™ÜÊCBƒBˆÈ[™HÜ[]™[K[Û™\ÚÝÈ^ŽˆÚ[™ÛK\ÚÝ[ÙKÝÝ]Hš[˜[BˆÈ™\ÜÛœÙHÛ›K›Ý[™È[ÙKˆž\\ÜÙ\ÈÛKœH[\™[KƒBˆYˆÙ]]Š\™ÜË›Û™\ÚÝ‹›Û™JNƒBˆÜ[—Ø[™Ù^]ÛÛ™\ÚÝ
Bˆ\™ÜË›Û™\ÚÝBˆ[Ù[YÙ]]Š\™ÜË›[Ù[‹›Û™JKBˆ›ÝšY\YÙ]]Š\™ÜËœ›ÝšY\ˆ‹›Û™JKBˆÛÛÙ]ÏYÙ]]Š\™ÜËÛÛÙ]È‹›Û™JKBˆ\ØYÙWÙš[OYÙ]]Š\™ÜË\ØYÙWÙš[H‹›Û™JKBˆ
CBƒBˆÈ[™HÜ[]™[K\™\Ý[YHÈKXÛÛ[YH\ÈÚÜÝ]ÈÚ]BˆYˆ
\™ÜËœ™\Ý[YHÜˆ\™ÜË˜ÛÛ[YWÛ\Ý
H[™\™ÜË˜ÛÛ[X[™\È›Û™NƒBˆ\™ÜË˜ÛÛ[X[™H˜Ú]ƒBˆ›Üˆ]‹Y˜][[ˆÃBˆ
œ]Y\žH‹›Û™JKBˆ
›[Ù[‹›Û™JKBˆ
œ›ÝšY\ˆ‹›Û™JKBˆ
ÛÛÙ]È‹›Û™JKBˆ
™\˜›ÜÙH‹›Û™JKBˆ
ÛÜšÝ™YH‹˜[ÙJKBˆNƒBˆYˆ›Ý\Ø]Š\™ÜË]ŠNƒBˆÙ]]Š\™ÜË]‹Y˜][
CBˆÛYØÚ]
\™ÜÊCBˆ™]\›ƒBƒBˆÈY˜][ÈÚ]Yˆ›ÈÛÛ[X[™ÜXÚYšYYBˆYˆ\™ÜË˜ÛÛ[X[™\È›Û™NƒBˆ›Üˆ]‹Y˜][[ˆÃBˆ
œ]Y\žH‹›Û™JKBˆ
›[Ù[‹›Û™JKBˆ
œ›ÝšY\ˆ‹›Û™JKBˆ
ÛÛÙ]È‹›Û™JKBˆ
™\˜›ÜÙH‹›Û™JKBˆ
œ™\Ý[YH‹›Û™JKBˆ
˜ÛÛ[YWÛ\Ý‹›Û™JKBˆ
ÛÜšÝ™YH‹˜[ÙJKBˆNƒBˆYˆ›Ý\Ø]Š\™ÜË]ŠNƒBˆÙ]]Š\™ÜË]‹Y˜][
CBˆÛYØÚ]
\™ÜÊCBˆ™]\›ƒBƒBˆÈ^XÝ]HHÛÛ[X[™ˆ›ÜYØ]HH[™\‰ÜÈ™]\›ˆÛÙH\ÈCBˆÈ›ØÙ\ÜÈ^]ÛÙHÛÈÝX˜ÛÛ[X[™È]ÚYÛ˜[˜Z[\™H
K™ËƒBˆÈ\›Y\ÈYÜ™\ÜÈÝ\™Y\Ú[™ÈÚ[ˆÜ™Y[X[ÜÛÝ\˜ÙOXš]Ø\™[ƒBˆÈ\ÈZ\ØÛÛ™šYÝ\™Y
HXÝX[H^]›Û‹^™\›Ëˆ[™\œÈ]™]\›ƒBˆÈ›Û™H\™H™X]Y\ÈÝXØÙ\ÜÈ
^]
KƒBˆYˆ\Ø]Š\™ÜË™[˜ÈŠNƒBˆ˜ÈH\™ÜË™[˜Ê\™ÜÊCBˆYˆ\Ú[œÝ[˜ÙJ˜Ë[
H[™˜ÈOHƒBˆÞ\Ë™^]
˜ÊCBˆ[ÙNƒBˆ\œÙ\‹œš[Ú[

CBƒBƒBšYˆ×Û˜[YW×ÈOH—×ÛXZ[—×ÈŽƒBˆXZ[Š
CB