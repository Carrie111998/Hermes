#!/usr/bin/env python3
"""Fake a busy coding session in the terminal.

Four scenes cycle in random order: an editor typing source, a build, a test
run, and git activity. Every byte the scenes print is invented — no file is
read or written, no command runs, nothing touches the network. This is a joke
screensaver in the `cmatrix` tradition, not a tool.

    python3 busy_terminal.py                 # until Ctrl-C
    python3 busy_terminal.py --duration 120  # two minutes, then exit
    python3 busy_terminal.py --scene tests   # one scene on repeat
    python3 busy_terminal.py --window        # open a new terminal, return now

`--window` is the one thing here that starts a process: it re-launches this
script inside a fresh terminal window and exits. An agent needs it, because a
captured pipe has no TTY to animate and an unbounded run would never return.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence, TextIO

SCENES = ("code", "build", "tests", "git")

RESET = "\033[0m"
BOLD = "\033[1m"
GREY = "\033[38;5;244m"
RED = "\033[38;5;203m"
GREEN = "\033[38;5;114m"
YELLOW = "\033[38;5;180m"
BLUE = "\033[38;5;75m"
MAGENTA = "\033[38;5;176m"
CYAN = "\033[38;5;80m"
ORANGE = "\033[38;5;215m"

CLEAR_SCREEN = "\033[2J\033[H"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


# ── Console ──────────────────────────────────────────────────────────────────


class Console:
    """The only surface a scene is allowed to touch.

    Output and timing are injected rather than hardcoded to stdout and
    ``time.sleep`` so a test can drive an entire scene against a fake clock and
    capture the frames instead of sleeping through them.
    """

    def __init__(
        self,
        *,
        width: int = 100,
        height: int = 30,
        color: bool = True,
        speed: float = 1.0,
        write: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.width = max(40, width)
        self.height = max(10, height)
        self.color = color
        self.speed = speed if speed > 0 else 1.0
        self._write = write if write is not None else _stdout_writer
        self._sleep = sleep if sleep is not None else time.sleep

    def paint(self, text: str = "") -> None:
        """Emit text with no trailing newline."""
        self._write(text)

    def line(self, text: str = "") -> None:
        self._write(text + "\n")

    def pause(self, seconds: float) -> None:
        """Sleep, scaled by --speed. Never negative, never a busy-wait."""
        self._sleep(max(0.0, seconds) / self.speed)

    def tint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def clear(self) -> None:
        if self.color:
            self._write(CLEAR_SCREEN)


def _stdout_writer(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


# ── Pure formatters ──────────────────────────────────────────────────────────


def progress_bar(done: float, total: float, width: int = 28) -> str:
    """A fixed-width bar. Out-of-range input clamps instead of overflowing."""
    width = max(1, width)
    fraction = 0.0 if total <= 0 else done / total
    fraction = min(1.0, max(0.0, fraction))
    filled = round(fraction * width)

    return "█" * filled + "░" * (width - filled)


def human_bytes(count: float) -> str:
    """Byte count as a short human string (1536 -> '1.5 KiB')."""
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(count) < step or unit == "GiB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= step

    return f"{count:.1f} GiB"


def next_scene(rng: random.Random, last: str = "", scenes: Sequence[str] = SCENES) -> str:
    """Pick the next scene, never the one that just played.

    Back-to-back repeats are what make a shuffle look broken, so they are
    excluded rather than left to chance. A one-scene catalog still returns
    that scene — the rule yields rather than looping forever.
    """
    options = [scene for scene in scenes if scene != last] or list(scenes)

    return rng.choice(options)


def test_summary(passed: int, failed: int, skipped: int, seconds: float) -> str:
    """The pytest-style tail line. Failures lead when there are any."""
    parts = []
    if failed:
        parts.append(f"{failed} failed")
    parts.append(f"{passed} passed")
    if skipped:
        parts.append(f"{skipped} skipped")

    return f"{', '.join(parts)} in {seconds:.2f}s"


# ── Syntax highlighting ──────────────────────────────────────────────────────

KEYWORDS = {
    "python": {
        "async", "await", "class", "def", "elif", "else", "except", "finally",
        "for", "from", "if", "import", "in", "is", "not", "raise", "return",
        "try", "while", "with", "yield", "None", "True", "False",
    },
    "ts": {
        "async", "await", "const", "export", "function", "if", "import",
        "interface", "let", "new", "return", "type", "useEffect", "useState",
        "from", "null", "true", "false",
    },
    "go": {
        "defer", "err", "for", "func", "if", "import", "nil", "package",
        "range", "return", "struct", "type", "var",
    },
    "rust": {
        "as", "enum", "fn", "impl", "let", "match", "mod", "mut", "pub",
        "return", "self", "struct", "use", "while", "Some", "None", "Ok", "Err",
    },
}

_TOKEN = re.compile(
    r"(?P<comment>#.*$|//.*$)"
    r"|(?P<string>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
    r"|(?P<word>[A-Za-z_][A-Za-z_0-9]*)"
)


def highlight(line: str, language: str, color: bool = True) -> str:
    """Tint keywords, strings, comments, and numbers. A no-op without color."""
    if not color:
        return line

    keywords = KEYWORDS.get(language, set())

    def paint(match: re.Match[str]) -> str:
        text = match.group(0)
        kind = match.lastgroup
        if kind == "comment":
            return f"{GREY}{text}{RESET}"
        if kind == "string":
            return f"{GREEN}{text}{RESET}"
        if kind == "number":
            return f"{ORANGE}{text}{RESET}"
        if kind == "word" and text in keywords:
            return f"{MAGENTA}{text}{RESET}"

        return text

    return _TOKEN.sub(paint, line)


# ── Content ──────────────────────────────────────────────────────────────────

REPO = "~/work/atlas"

BRANCHES = ("feat/ingest-backoff", "fix/session-leak", "feat/live-query", "chore/deps")

SOURCES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "services/ingest/retry.py",
        "python",
        (
            "import asyncio",
            "import random",
            "from typing import Awaitable, Callable, TypeVar",
            "",
            'T = TypeVar("T")',
            "",
            "",
            "async def with_backoff(",
            "    call: Callable[[], Awaitable[T]],",
            "    attempts: int = 5,",
            "    base: float = 0.25,",
            ") -> T:",
            '    """Retry an awaitable with jittered exponential backoff."""',
            "    last = None",
            "    for attempt in range(attempts):",
            "        try:",
            "            return await call()",
            "        except TransientError as exc:",
            "            last = exc",
            "            delay = base * 2 ** attempt",
            "            # Jitter keeps a thundering herd from resynchronising.",
            "            await asyncio.sleep(delay + random.random() * base)",
            "    raise RetryExhausted(attempts) from last",
        ),
    ),
    (
        "web/src/hooks/use-live-query.ts",
        "ts",
        (
            "import { useEffect, useState } from 'react'",
            "",
            "interface LiveQuery<T> {",
            "  data: T | null",
            "  error: Error | null",
            "  stale: boolean",
            "}",
            "",
            "export function useLiveQuery<T>(key: string): LiveQuery<T> {",
            "  const [data, setData] = useState<T | null>(null)",
            "  const [error, setError] = useState<Error | null>(null)",
            "",
            "  useEffect(() => {",
            "    let cancelled = false",
            "    subscribe(key, next => {",
            "      // A late frame must never overwrite newer intent.",
            "      if (!cancelled) setData(next)",
            "    }).catch(setError)",
            "",
            "    return () => {",
            "      cancelled = true",
            "    }",
            "  }, [key])",
            "",
            "  return { data, error, stale: data === null }",
            "}",
        ),
    ),
    (
        "internal/api/handler.go",
        "go",
        (
            "package api",
            "",
            "import (",
            '    "encoding/json"',
            '    "net/http"',
            '    "time"',
            ")",
            "",
            "func (s *Server) handleSnapshot(w http.ResponseWriter, r *http.Request) {",
            "    ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)",
            "    defer cancel()",
            "",
            "    snap, err := s.store.Snapshot(ctx, chi.URLParam(r, \"id\"))",
            "    if err != nil {",
            "        s.fail(w, http.StatusBadGateway, err)",
            "        return",
            "    }",
            "",
            "    w.Header().Set(\"Cache-Control\", \"no-store\")",
            "    json.NewEncoder(w).Encode(snap)",
            "}",
        ),
    ),
    (
        "crates/parser/src/lexer.rs",
        "rust",
        (
            "use std::str::Chars;",
            "",
            "pub struct Lexer<'a> {",
            "    chars: Chars<'a>,",
            "    offset: usize,",
            "}",
            "",
            "impl<'a> Lexer<'a> {",
            "    pub fn next_token(&mut self) -> Option<Token> {",
            "        let start = self.offset;",
            "        match self.bump()? {",
            "            c if c.is_whitespace() => self.next_token(),",
            "            // A bare '-' is a minus; '->' is an arrow.",
            "            '-' if self.peek() == Some('>') => Some(Token::Arrow),",
            "            c if c.is_ascii_digit() => Some(self.number(start)),",
            "            _ => Some(Token::Unknown(start)),",
            "        }",
            "    }",
            "}",
        ),
    ),
)

BUILDS = (
    (
        "npm run build",
        "vite v5.4.11 building for production...",
        (
            "web/assets/index-4f21ac.js",
            "web/assets/vendor-9cb0e1.js",
            "web/assets/index-0b7714.css",
        ),
    ),
    (
        "cargo build --release",
        "   Compiling atlas-parser v0.9.3",
        (
            "target/release/atlas",
            "target/release/atlas-parser.rlib",
        ),
    ),
    (
        "docker build -t atlas/ingest:dev .",
        "=> [internal] load build definition from Dockerfile",
        (
            "layer sha256:9c1b4f2a",
            "layer sha256:2fd0aa71",
        ),
    ),
)

TEST_FILES = (
    "tests/ingest/test_backoff.py",
    "tests/api/test_snapshot.py",
    "tests/store/test_lineage.py",
    "tests/web/use-live-query.test.ts",
    "tests/parser/test_lexer.py",
)

COMMIT_SUBJECTS = (
    "fix(ingest): jitter the backoff so retries stop resynchronising",
    "feat(api): cache-bust snapshot responses behind a short timeout",
    "refactor(parser): fold the arrow case into next_token",
    "fix(web): drop a late frame instead of overwriting newer state",
    "test(store): pin the lineage invariant across compression",
)

CI_CHECKS = (
    "lint / ruff",
    "tests / python 3.12",
    "tests / node 22",
    "build / linux-amd64",
    "supply-chain / audit",
)


# ── Scenes ───────────────────────────────────────────────────────────────────


def type_out(
    console: Console,
    text: str,
    *,
    prefix: str = "",
    language: str = "",
    rng: random.Random,
) -> None:
    """Type a line a character at a time, then repaint it highlighted.

    Highlighting a partial line would recolor tokens as they grow, which reads
    as flicker; typing plain and repainting once at the end looks like an
    editor catching up. The repaint returns to column 0, so it has to redraw
    `prefix` (the line-number gutter) or the code slides left over it.
    """
    if prefix:
        console.paint(prefix)

    for char in text:
        console.paint(char)
        console.pause(rng.uniform(0.004, 0.028) if char != " " else 0.006)

    if language and console.color:
        console.paint("\r" + prefix + highlight(text, language, console.color))
    console.line()


def prompt(console: Console, command: str, rng: random.Random) -> None:
    """The shell prompt plus a typed-out command."""
    console.paint(console.tint(REPO, BLUE) + console.tint(" ❯ ", GREEN))
    for char in command:
        console.paint(char)
        console.pause(rng.uniform(0.012, 0.05))
    console.line()
    console.pause(0.35)


def scene_code(console: Console, rng: random.Random) -> None:
    """An editor pane filling with source, line numbers and all."""
    path, language, lines = rng.choice(SOURCES)

    console.clear()
    console.line(console.tint(f"  {path}", BOLD) + console.tint("   ● unsaved", ORANGE))
    console.line(console.tint("  " + "─" * (console.width - 4), GREY))

    for number, text in enumerate(lines, start=1):
        gutter = console.tint(f"{number:>4} │ ", GREY)
        type_out(console, text, prefix=gutter, language=language, rng=rng)

        # Every so often, stop and stare at it like a person would.
        if rng.random() < 0.12:
            console.pause(rng.uniform(0.4, 1.1))

    console.line()
    console.pause(0.5)
    console.line(console.tint("  saved", GREEN) + console.tint(f"  {path}", GREY))
    console.pause(1.2)


def scene_build(console: Console, rng: random.Random) -> None:
    """A build with staged progress and an artifact table."""
    command, banner, artifacts = rng.choice(BUILDS)

    console.line()
    prompt(console, command, rng)
    console.line(console.tint(banner, GREY))

    total = rng.randint(180, 940)
    step = max(1, total // rng.randint(12, 20))
    done = 0
    while done < total:
        done = min(total, done + step)
        bar = progress_bar(done, total)
        console.paint(f"\r{console.tint(bar, CYAN)} {done}/{total} modules")
        console.pause(rng.uniform(0.05, 0.16))

    console.line()
    console.line()
    for artifact in artifacts:
        size = rng.uniform(12_000, 940_000)
        gzip = size / rng.uniform(2.8, 4.1)
        console.line(
            console.tint(f"  {artifact:<34}", GREY)
            + console.tint(f"{human_bytes(size):>10}", YELLOW)
            + console.tint(f"  │ gzip: {human_bytes(gzip):>9}", GREY)
        )
        console.pause(0.14)

    console.line()
    console.line(console.tint(f"  ✓ built in {rng.uniform(3.2, 24.0):.2f}s", GREEN))
    console.pause(1.4)


def scene_tests(console: Console, rng: random.Random) -> None:
    """A test run that mostly passes, occasionally retries a flake."""
    console.line()
    prompt(console, "pytest -q", rng)

    passed = 0
    failed = 0
    skipped = 0
    for path in rng.sample(TEST_FILES, k=rng.randint(3, len(TEST_FILES))):
        console.paint(console.tint(f"  {path:<38}", GREY))
        for _ in range(rng.randint(6, 26)):
            roll = rng.random()
            if roll < 0.02:
                failed += 1
                console.paint(console.tint("F", RED))
            elif roll < 0.05:
                skipped += 1
                console.paint(console.tint("s", YELLOW))
            else:
                passed += 1
                console.paint(console.tint(".", GREEN))
            console.pause(rng.uniform(0.01, 0.07))
        console.line()

    seconds = rng.uniform(1.8, 19.4)
    console.line()
    summary = test_summary(passed, failed, skipped, seconds)
    console.line(console.tint(f"  {summary}", RED if failed else GREEN))

    if failed:
        console.pause(0.8)
        console.line(console.tint("  rerunning the failure in isolation…", GREY))
        console.pause(rng.uniform(1.0, 2.0))
        console.line(console.tint("  ✓ passed on retry — flake, not a break", GREEN))

    console.pause(1.4)


def scene_git(console: Console, rng: random.Random) -> None:
    """Commit, push, then CI checks going green one at a time."""
    branch = rng.choice(BRANCHES)
    subject = rng.choice(COMMIT_SUBJECTS)
    sha = "".join(rng.choice("0123456789abcdef") for _ in range(7))

    console.line()
    prompt(console, f'git commit -am "{subject}"', rng)
    files = rng.randint(2, 9)
    console.line(console.tint(f"[{branch} {sha}]", GREY) + f" {subject}")
    console.line(
        console.tint(
            f" {files} files changed, "
            f"{rng.randint(18, 240)} insertions(+), {rng.randint(3, 90)} deletions(-)",
            GREY,
        )
    )
    console.pause(0.9)

    prompt(console, f"git push origin {branch}", rng)
    objects = rng.randint(14, 61)
    for label, count in (("Counting objects", objects), ("Compressing objects", objects // 2)):
        for index in range(1, count + 1):
            percent = round(index / count * 100)
            console.paint(f"\r{console.tint(label, GREY)}: {percent:>3}% ({index}/{count})")
            console.pause(0.012)
        console.line(", done.")

    written = rng.uniform(2_000, 96_000)
    console.line(
        console.tint("Writing objects", GREY)
        + f": 100% ({objects}/{objects}), {human_bytes(written)}, done."
    )
    console.pause(0.6)
    console.line(console.tint(f"remote: Resolving deltas: 100% ({objects}/{objects}), done.", GREY))
    console.line("To github.com:atlas/atlas.git")
    console.line(console.tint(f"   {sha}..{sha[::-1]}  {branch} -> {branch}", GREY))
    console.line()

    console.pause(0.8)
    console.line(console.tint("  CI", BOLD))
    for check in rng.sample(CI_CHECKS, k=rng.randint(3, len(CI_CHECKS))):
        console.paint(console.tint(f"    ● {check}", YELLOW))
        console.pause(rng.uniform(0.5, 1.6))
        console.paint("\r" + console.tint(f"    ✓ {check}", GREEN) + "\n")

    console.pause(1.4)


SCENE_RUNNERS: dict[str, Callable[[Console, random.Random], None]] = {
    "code": scene_code,
    "build": scene_build,
    "tests": scene_tests,
    "git": scene_git,
}


def run(
    console: Console,
    rng: random.Random,
    *,
    scene: str = "",
    duration: float = 0.0,
    now: Callable[[], float] = time.monotonic,
) -> int:
    """Cycle scenes until `duration` elapses. Returns how many ran.

    `duration <= 0` means forever, so the caller (not this loop) owns the exit
    condition — Ctrl-C in the CLI, a fixed scene count in a test.
    """
    started = now()
    played = 0
    last = ""

    while True:
        last = scene or next_scene(rng, last)
        SCENE_RUNNERS[last](console, rng)
        played += 1

        if duration > 0 and now() - started >= duration:
            return played


# ── Launching a visible window ───────────────────────────────────────────────

LINUX_TERMINALS = (
    ("x-terminal-emulator", ("-e",)),
    ("gnome-terminal", ("--",)),
    ("konsole", ("-e",)),
    ("xfce4-terminal", ("-e",)),
    ("alacritty", ("-e",)),
    ("kitty", ("--",)),
    ("xterm", ("-e",)),
)


class NoTerminalError(RuntimeError):
    """No terminal emulator on this machine can host the screensaver."""


def applescript_string(text: str) -> str:
    """Quote text as an AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def window_argv(
    command: str,
    platform: str,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """Build the argv that runs `command` in a NEW visible terminal window.

    Takes the platform as data rather than reading `sys.platform`, so all three
    branches are checkable from one host.
    """
    if platform == "darwin":
        return [
            "osascript",
            "-e", f"tell application \"Terminal\" to do script {applescript_string(command)}",
            "-e", 'tell application "Terminal" to activate',
        ]

    if platform == "win32":
        return ["cmd", "/c", "start", "", "cmd", "/k", command]

    for emulator, flags in LINUX_TERMINALS:
        if which(emulator):
            # `sh -c` normalises the argument shape across emulators.
            return [emulator, *flags, "sh", "-c", command]

    raise NoTerminalError(
        "no terminal emulator found (tried: "
        + ", ".join(name for name, _ in LINUX_TERMINALS)
        + ")"
    )


def relaunch_command(argv: Sequence[str], script: str = "", python: str = "") -> str:
    """The shell command that re-runs this script without `--window`."""
    parts = [
        python or sys.executable,
        script or str(Path(__file__).resolve()),
        *[arg for arg in argv if arg != "--window"],
    ]

    return " ".join(shlex.quote(part) for part in parts)


def open_in_window(
    argv: Sequence[str],
    *,
    platform: str = sys.platform,
    spawn: Callable[..., object] = subprocess.Popen,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    """Start the screensaver in its own window and return immediately."""
    command = window_argv(relaunch_command(argv), platform, which)
    spawn(command)

    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def supports_color(stream: TextIO, requested: bool) -> bool:
    """Colour when asked for it, the stream is a TTY, and NO_COLOR is unset."""
    if not requested or os.environ.get("NO_COLOR"):
        return False

    return bool(getattr(stream, "isatty", lambda: False)())


def _enable_windows_ansi() -> None:
    """Turn on VT processing so the escapes mean something on Windows."""
    if sys.platform != "win32":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        # An old console just gets no colour; the animation still runs.
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="busy_terminal.py",
        description="A joke screensaver that fakes a busy coding session.",
    )
    parser.add_argument(
        "--duration", type=float, default=0.0,
        help="Seconds to run. 0 (default) runs until Ctrl-C.",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Time multiplier. 2 is twice as fast, 0.5 half.",
    )
    parser.add_argument(
        "--scene", choices=SCENES, default="",
        help="Play one scene on repeat instead of cycling.",
    )
    parser.add_argument("--seed", type=int, default=None, help="Seed for a reproducible run.")
    parser.add_argument("--no-color", action="store_true", help="Plain text, no ANSI escapes.")
    parser.add_argument(
        "--window", action="store_true",
        help="Open a new terminal window running this, then exit. Use this from an agent.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw)

    if args.window:
        return open_in_window(raw)

    _enable_windows_ansi()

    size = shutil.get_terminal_size(fallback=(100, 30))
    console = Console(
        width=size.columns,
        height=size.lines,
        color=supports_color(sys.stdout, not args.no_color),
        speed=args.speed,
    )
    rng = random.Random(args.seed)

    if console.color:
        console.paint(HIDE_CURSOR)
    try:
        run(console, rng, scene=args.scene, duration=args.duration)
    except KeyboardInterrupt:
        pass
    finally:
        if console.color:
            console.paint(SHOW_CURSOR + RESET)
        console.line()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
