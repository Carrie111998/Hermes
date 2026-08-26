"""Verified front-raising for macOS ``open`` invocations (#95261).

On macOS, a file opened from a Hermes tool call (``open -a Preview x.pdf``,
``open x.pdf``) can load successfully while its window lands BEHIND the
Hermes desktop window: the tool shell is a child process of the (typically
maximised) Electron app, and macOS silently ignores activation hand-off
from a non-frontmost process. ``open`` still exits 0, so the agent reports
success while the user sees nothing happen — the exact false-success shape
the masked-success backstop in ``terminal_hints`` guards against elsewhere.

This module adds a *verification* tier for exit-0 ``open`` commands
(advisory only — the exit code itself is never modified):

1. Settle briefly (~0.8s), then ask System Events which app is frontmost.
2. If the requested app is not frontmost, escalate through a ladder of
   window-layering options, each VERIFIED before moving on:

   a. ``osascript -e 'tell application "X" to activate'``
   b. ``lsappinfo setfront <asn>`` — a different subsystem; works when
      AppleEvents are swallowed
   c. re-issue the original ``open`` argv verbatim — macOS treats
      re-opening an already-open document as pure activation
   d. reopen + activate (un-minimises windows, then raises)

3. Report ONLY from the final observed ``frontmost`` state — never from
   "a rung returned 0" (any rung can exit 0 while focus lands elsewhere).
   After an apparent raise, settle once more and re-assert that rung a
   single time so a queued activation from another app can't steal the
   verdict. A focus race gets its own distinct outcome
   (``focus_elsewhere``) so it can never read as success.

Design rules (mirroring ``tools/terminal_hints.py``):

* Only fires on exit-0 results whose command actually invokes ``open``.
* Honors explicit ``-g``/``--background`` — if the caller asked for a
  background launch there is nothing to verify.
* Every argv is built by a pure function so tests can mock the runner and
  assert exactly what would be executed.
* Nothing here raises into tool dispatch; all failures degrade to silence
  (no note) or an honest advisory note.
"""

from __future__ import annotations

import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

# How long to wait after a raise attempt before checking `frontmost`,
# absorbing window-server latency and queued activations.
SETTLE_SECONDS = 0.8

# Per-subprocess cap. The whole ladder is bounded (~4 rungs × (cmd + 2
# checks)); the common swallowed-activation case costs one activate round.
_TOOL_TIMEOUT_SECONDS = 3.0

# Process names that mean "our own desktop app kept focus" for BARE opens
# (no ``-a`` given, so the target app is unknown until we observe it).
_HERMES_FRONT_NAMES = frozenset({"hermes", "electron"})

Runner = Callable[[List[str], float], "RunResult"]

# Control tokens that end an invocation's own argument list.
_CONTROL_TOKENS = frozenset({"&", "|", ";", "&&", "||", ">", "<", ">>", "<<"})

_ASN_RE = re.compile(r'"ASN"\s*=\s*"([^"]+)"')
_ASN_LOOSE_RE = re.compile(r"0x[0-9a-fA-F]+-0x[0-9a-fA-F]+")


class RunResult:
    """Minimal subprocess outcome the injectable runner must return."""

    __slots__ = ("returncode", "stdout")

    def __init__(self, returncode: int = 0, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout or ""


@dataclass
class OpenInvocation:
    """One parsed ``open ...`` invocation from a shell command."""

    argv: List[str]

    @property
    def app(self) -> Optional[str]:
        """Value of ``-a``/``--apps`` when present."""
        args = self.argv[1:]
        for i, tok in enumerate(args):
            if tok in ("-a", "--apps") and i + 1 < len(args):
                return args[i + 1]
        return None

    @property
    def wants_background(self) -> bool:
        """True when the caller explicitly asked NOT to come to front."""
        return bool({"-g", "--background"} & set(self.argv[1:]))

    @property
    def reveal_target(self) -> Optional[str]:
        """``-R`` reveals in Finder; the effective target app is Finder."""
        if "-R" in self.argv[1:] or "--reveal" in self.argv[1:]:
            return "Finder"
        return None


@dataclass
class RaiseOutcome:
    """Final verdict, reported strictly from observed ``frontmost`` state.

    ``status`` is one of:

    * ``fronted``        — the target app was observed frontmost
    * ``focus_elsewhere``— file opened but another app held focus after
      every verified raise attempt (a focus race; NEVER report success)
    * ``unverified``     — checks worked earlier, then stopped answering;
      honest "could not confirm" rather than a silent pass
    * ``silent``         — no observation was ever possible (osascript
      missing/unhappy); callers treat this as "no note"
    """

    status: str
    frontmost: Optional[str] = None
    confirmed: bool = False
    attempts: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure argv builders — the "pass the right arguments" contract under test.
# ---------------------------------------------------------------------------


def _applescript_quote(value: str) -> str:
    """Escape a value for embedding in a double-quoted AppleScript string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_frontmost_check_argv() -> List[str]:
    """Ask System Events which process is frontmost right now."""
    return [
        "osascript",
        "-e",
        'tell application "System Events" to get name of first application '
        "process whose frontmost is true",
    ]


def build_activate_argv(app: str) -> List[str]:
    """Ladder rung a: AppleEvents activation."""
    script = f'tell application "{_applescript_quote(app)}" to activate'
    return ["osascript", "-e", script]


def build_lsappinfo_find_argv(app: str) -> List[str]:
    """Resolve the app's ASN (application serial number) via LaunchServices."""
    return ["lsappinfo", "find", "-only", "asn", f"LSDisplayName={app}"]


def build_lsappinfo_setfront_argv(asn: str) -> List[str]:
    """Ladder rung b: force front status via the LaunchServices subsystem."""
    return ["lsappinfo", "setfront", asn]


def build_reissue_open_argv(invocation: OpenInvocation) -> List[str]:
    """Ladder rung c: re-issue the original open verbatim (pure activation
    on an already-open document)."""
    return list(invocation.argv)


def build_reopen_activate_argv(app: str) -> List[str]:
    """Ladder rung d: un-minimise the app's windows, then activate."""
    quoted = _applescript_quote(app)
    return [
        "osascript",
        "-e",
        f'tell application "{quoted}" to reopen',
        "-e",
        f'tell application "{quoted}" to activate',
    ]


def parse_asn(stdout: str) -> Optional[str]:
    """Best-effort ASN extraction from ``lsappinfo find -only asn`` output.

    Handles both the keyed form (``"ASN"="0x0-0x1e06d Preview"``) and older
    builds that print the bare ASN. Returns None when nothing ASN-shaped is
    found — the caller skips the rung rather than passing garbage.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    m = _ASN_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _ASN_LOOSE_RE.search(text)
    if m:
        return m.group(0)
    return None


# ---------------------------------------------------------------------------
# Command parsing — locate the ``open`` invocation a tool call performed.
# ---------------------------------------------------------------------------


def _split_segments(command: str) -> List[str]:
    """Split a compound command on top-level ``&&``/``||``/``;``.

    Quote-aware so ``cd "/a b" && open x.pdf`` splits cleanly even though
    the quoted path contains spaces.
    """
    segments: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        two = command[i : i + 2]
        if two in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == ";":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _invocation_from_segment(segment: str) -> Optional[OpenInvocation]:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None
    # Skip leading VAR=... environment assignments and wrapping sudo.
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if "=" in tok and not tok.startswith(("./", "/")) and idx == 0 or (
            tok == "sudo"
        ):
            idx += 1
            continue
        break
    if idx >= len(tokens):
        return None
    prog = tokens[idx].rsplit("/", 1)[-1]
    if prog != "open":
        return None
    # Trim anything past open's own args (redirections, background '&').
    argv = [tokens[idx]]
    for tok in tokens[idx + 1 :]:
        if tok in _CONTROL_TOKENS:
            break
        argv.append(tok)
    return OpenInvocation(argv=argv)


def parse_open_invocation(command: str) -> Optional[OpenInvocation]:
    """Return the LAST ``open ...`` invocation in ``command``, or None.

    The last one wins because it owns whatever window the user ends up
    looking for. Returns None for commands that never invoke ``open`` —
    this is the hot path (every successful terminal call), so it must stay
    pure and cheap.
    """
    if not command or "open" not in command:
        return None
    found: Optional[OpenInvocation] = None
    for segment in _split_segments(command):
        inv = _invocation_from_segment(segment)
        if inv is not None:
            found = inv
    return found


def _effective_app(invocation: OpenInvocation) -> Optional[str]:
    """The app whose frontness proves visibility, when knowable.

    ``-a X`` names it directly; ``-R`` targets Finder; a bare ``open``
    leaves it unknown (the caller falls back to the Hermes-front heuristic).
    """
    return invocation.app or invocation.reveal_target


def _names_match(frontmost: str, app: str) -> bool:
    """Case-insensitive process-name match with bundle-id tolerance.

    ``-a com.apple.Preview`` must count when Preview is frontmost: derive
    the bare-name candidate from the last dot-segment.
    """
    candidates = {app.casefold()}
    if "." in app and " " not in app:
        candidates.add(app.rsplit(".", 1)[-1].casefold())
    return frontmost.casefold() in candidates


# ---------------------------------------------------------------------------
# Default runner (injected in tests).
# ---------------------------------------------------------------------------


def _default_run(argv: List[str], timeout: float) -> RunResult:
    import subprocess

    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return RunResult(completed.returncode, completed.stdout or "")


def _frontmost_app(run: Runner) -> Optional[str]:
    """Current frontmost process name, or None when unanswerable."""
    try:
        result = run(build_frontmost_check_argv(), _TOOL_TIMEOUT_SECONDS)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    name = (result.stdout or "").strip().strip('"').strip()
    return name or None


# ---------------------------------------------------------------------------
# Ladder actions. Each is attempted once and its RESULT IS DELIBERATELY
# IGNORED (#95261): every one of these can exit 0 while focus lands
# elsewhere, so the follow-up frontmost observation is the only verdict.
# ---------------------------------------------------------------------------


def _act_activate(run: Runner, app: str) -> None:
    run(build_activate_argv(app), _TOOL_TIMEOUT_SECONDS)


def _act_setfront(run: Runner, app: str) -> None:
    try:
        find_result = run(build_lsappinfo_find_argv(app), _TOOL_TIMEOUT_SECONDS)
    except Exception:
        return
    asn = parse_asn(find_result.stdout) if find_result.returncode == 0 else None
    if not asn:
        return
    try:
        run(build_lsappinfo_setfront_argv(asn), _TOOL_TIMEOUT_SECONDS)
    except Exception:
        pass


def _act_reissue(run: Runner, argv: List[str]) -> None:
    run(list(argv), _TOOL_TIMEOUT_SECONDS)


def _act_reopen(run: Runner, app: str) -> None:
    run(build_reopen_activate_argv(app), _TOOL_TIMEOUT_SECONDS)


def _target_reached(frontmost: Optional[str], app: Optional[str]) -> bool:
    if frontmost is None:
        return False
    if app is not None:
        return _names_match(frontmost, app)
    # Bare open: success means focus MOVED OFF Hermes onto something else.
    return frontmost.casefold() not in _HERMES_FRONT_NAMES


def verify_open_in_front(
    app: Optional[str],
    open_argv: List[str],
    *,
    run: Runner,
    sleep: Callable[[float], None] = time.sleep,
    settle: float = SETTLE_SECONDS,
) -> RaiseOutcome:
    """Run the verified raise ladder for one completed ``open``.

    Never trusts a rung's exit code: every verdict comes from a fresh
    ``frontmost`` observation taken after ``settle`` seconds. An apparently
    successful raise is confirmed once (settle → re-check → single
    re-assertion) so a queued activation from another app cannot steal the
    verdict (#95261).
    """
    attempts: List[str] = []

    def observe() -> Optional[str]:
        sleep(settle)
        return _frontmost_app(run)

    final_front = observe()
    if final_front is None:
        # osascript/System Events unavailable — say nothing rather than
        # annotate every open on a headless box.
        return RaiseOutcome("silent")

    if _target_reached(final_front, app):
        return RaiseOutcome("fronted", final_front, confirmed=False)

    ladder: List[tuple] = []
    if app is not None:
        ladder.append(("activate", lambda: _act_activate(run, app)))
        ladder.append(("lsappinfo-setfront", lambda: _act_setfront(run, app)))
    ladder.append(("reissue-open", lambda: _act_reissue(run, open_argv)))
    if app is not None:
        ladder.append(("reopen-activate", lambda: _act_reopen(run, app)))

    for label, action in ladder:
        attempts.append(label)
        try:
            action()
        except Exception:
            # A raising rung carries no information; the observation below
            # is still taken so we report the world as it is.
            pass
        observed = observe()
        if observed is None:
            # Checks worked at least once (we have ``final_front``), then
            # stopped answering — an honest "could not confirm", not a
            # silent pass.
            if final_front is not None:
                return RaiseOutcome("unverified", final_front, attempts=attempts)
            return RaiseOutcome("silent")
        final_front = observed
        if _target_reached(observed, app):
            # Confirm once: another app may hold a queued activation.
            confirmed = observe()
            if confirmed is not None and _target_reached(confirmed, app):
                return RaiseOutcome(
                    "fronted", confirmed, confirmed=True, attempts=attempts
                )
            # Lost the raise to a focus race — re-assert THIS rung once.
            if confirmed is not None:
                try:
                    action()
                except Exception:
                    pass
                reasserted = observe()
                if reasserted is not None and _target_reached(reasserted, app):
                    return RaiseOutcome(
                        "fronted",
                        reasserted,
                        confirmed=True,
                        attempts=attempts,
                    )
                if reasserted is None:
                    return RaiseOutcome("unverified", final_front, attempts=attempts)
                final_front = reasserted
            continue
        final_front = observed

    return RaiseOutcome("focus_elsewhere", final_front, attempts=attempts)


# ---------------------------------------------------------------------------
# Tool-facing entry point.
# ---------------------------------------------------------------------------


def annotate_macos_open_success(
    command: str,
    *,
    env_type: str = "local",
    platform: Optional[str] = None,
    run: Optional[Runner] = None,
    sleep: Callable[[float], None] = time.sleep,
    settle: float = SETTLE_SECONDS,
) -> Optional[str]:
    """Advisory note for an exit-0 command that ran ``open`` on macOS.

    Called by ``terminal_tool`` next to the masked-success backstop.
    Returns a short honest note describing the FINAL OBSERVED state, or
    None when there is nothing to say (non-darwin, remote/container env,
    not an open command, background launch requested, or verification was
    impossible from the start).
    """
    platform = platform or sys.platform
    if platform != "darwin":
        return None
    if (env_type or "local") != "local":
        # Commands running in Docker/SSH/Modal land on another machine; our
        # local osascript would describe the wrong session.
        return None
    invocation = parse_open_invocation(command)
    if invocation is None or invocation.wants_background:
        return None

    app = _effective_app(invocation)
    runner = run or _default_run
    outcome = verify_open_in_front(
        app,
        build_reissue_open_argv(invocation),
        run=runner,
        sleep=sleep,
        settle=settle,
    )

    target = app if app is not None else "the document"
    if outcome.status == "fronted":
        if outcome.confirmed:
            return (
                f"NOTE(macOS): plain exit 0 did not prove visibility — the "
                f"'{target}' window had opened behind Hermes; the verified "
                f"raise ladder brought it to front "
                f"(confirmed frontmost='{outcome.frontmost}')."
            )
        return (
            f"NOTE(macOS): '{target}' opened and is frontmost "
            f"(observed frontmost='{outcome.frontmost}'); safe to report "
            f"as visible."
        )
    if outcome.status == "focus_elsewhere":
        tried = ", ".join(outcome.attempts) or "no rungs"
        return (
            f"WARNING(macOS): FILE IS OPEN BUT NOT IN FRONT — '{target}' "
            f"loaded, but '{outcome.frontmost}' holds focus after the "
            f"verified raise ladder ({tried}). Do NOT report this as a "
            f"visible success: tell the user the document is open behind "
            f"other windows and suggest clicking its Dock icon."
        )
    if outcome.status == "unverified":
        return (
            f"NOTE(macOS): '{target}' was opened, but focus verification "
            f"stopped answering mid-ladder (last observed frontmost="
            f"'{outcome.frontmost}'). Confirm the window is actually "
            f"visible before reporting success."
        )
    return None  # silent: nothing was ever observable
