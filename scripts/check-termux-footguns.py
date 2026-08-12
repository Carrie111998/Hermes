#!/usr/bin/env python3
"""Fast static guard for Hermes' native Termux/Android support.

This complements the normal Python/TypeScript tests with cheap checks for
regressions that are easy to introduce on a Linux CI host but fail immediately
in Android/Bionic Termux: Linux distribution package managers, leaked fake
Python platform tags, accidentally executing Electron on Android, unsafe X11
Chromium flags, and widening the browser-hosted Desktop beyond loopback.

Usage:
    python scripts/check-termux-footguns.py             # staged files
    python scripts/check-termux-footguns.py --all       # full repository audit
    python scripts/check-termux-footguns.py --diff main # files changed vs main
    python scripts/check-termux-footguns.py path ...    # explicit files/dirs

Intentional matches can be suppressed on the same line with:
    # termux-footgun: ok - <reason>
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPRESS_MARKER = re.compile(r"#\s*termux-footgun\s*:\s*ok\b", re.IGNORECASE)
TEXT_SUFFIXES = {".py", ".sh", ".ts", ".tsx", ".js", ".mjs", ".json", ".yml", ".yaml"}
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "release",
    "__pycache__",
    ".pytest_cache",
}
EXCLUDED_FILES = {"scripts/check-termux-footguns.py"}


@dataclass(frozen=True)
class Footgun:
    name: str
    paths: tuple[str, ...]
    pattern: re.Pattern[str]
    message: str
    fix: str


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    text: str
    name: str
    message: str
    fix: str


FOOTGUNS: tuple[Footgun, ...] = (
    Footgun(
        name="host Linux package manager in native Termux installer",
        paths=("scripts/install-termux.sh",),
        pattern=re.compile(r"^\s*(?:sudo\s+|apt(?:-get)?\s+|systemctl\s+)", re.IGNORECASE),
        message=(
            "Native Termux has no sudo/apt/systemd host layer. Installing host-Linux "
            "packages here makes the native Android path silently depend on proot."
        ),
        fix="Use `pkg`/`apt` only through Termux's package environment, normally `pkg install ...`.",
    ),
    Footgun(
        name="fake _PYTHON_HOST_PLATFORM assignment",
        paths=("scripts/install-termux.sh",),
        pattern=re.compile(r"^\s*(?:export\s+)?_PYTHON_HOST_PLATFORM\s*="),
        message=(
            "Forcing _PYTHON_HOST_PLATFORM makes resolvers select non-Android wheel tags. "
            "The native installer must derive Android/Bionic tags from the interpreter."
        ),
        fix="Remove the assignment; explicitly `unset _PYTHON_HOST_PLATFORM` before resolution.",
    ),
    Footgun(
        name="Chromium --no-sandbox in Termux Desktop transport",
        paths=("hermes_cli/termux_desktop.py",),
        pattern=re.compile(r"[\"']--no-sandbox(?:[=\"']|\b)"),
        message=(
            "The Termux:X11 Chromium transport must not normalize --no-sandbox into its "
            "launcher. Keep Chromium's Android/Termux sandbox policy intact."
        ),
        fix="Remove --no-sandbox from the Termux Chromium command.",
    ),
    Footgun(
        name="privileged Termux PR regression container",
        paths=(".github/workflows/termux-regression.yml",),
        pattern=re.compile(r"(?:^|\s)--privileged(?:\s|$)|--security-opt(?:=|\s+)seccomp=unconfined"),
        message=(
            "PR-controlled Termux regression code must not run in a privileged or "
            "seccomp-disabled Docker container on the GitHub runner."
        ),
        fix="Use an unprivileged container and add only a narrowly justified capability if a real gate proves it is necessary.",
    ),
    Footgun(
        name="wildcard bind in Termux Desktop transport",
        paths=("hermes_cli/termux_desktop.py",),
        pattern=re.compile(r"[\"'](?:0\.0\.0\.0|::)[\"']"),
        message="Termux Desktop is a same-device surface and must remain loopback-only.",
        fix="Bind the Desktop server to 127.0.0.1 and expose only its localhost URL.",
    ),
)


def _repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def should_scan_file(path: Path) -> bool:
    rel = _repo_rel(path)
    if rel in EXCLUDED_FILES or path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    return not any(part in EXCLUDED_DIRS for part in Path(rel).parts)


def iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for raw in paths:
        path = raw if raw.is_absolute() else REPO_ROOT / raw
        if path.is_file():
            if should_scan_file(path) and path not in seen:
                seen.add(path)
                yield path
            continue
        if not path.is_dir():
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for name in files:
                candidate = Path(root) / name
                if candidate not in seen and should_scan_file(candidate):
                    seen.add(candidate)
                    yield candidate


def _line_code(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return ""
    # Shell/Python comments are enough for the narrow policy rules above. Keep
    # URL fragments in strings intact by treating ` #` as the comment boundary.
    marker = line.find(" #")
    return line if marker < 0 else line[:marker]


def scan_file(path: Path) -> list[Finding]:
    rel = _repo_rel(path)
    applicable = [rule for rule in FOOTGUNS if rel in rule.paths]
    if not applicable:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), 1):
        if SUPPRESS_MARKER.search(line):
            continue
        code = _line_code(line)
        for rule in applicable:
            if rule.pattern.search(code):
                findings.append(
                    Finding(path, number, line.rstrip(), rule.name, rule.message, rule.fix)
                )
    return findings


def _function_source(path: Path, name: str) -> tuple[str, int] | None:
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (OSError, SyntaxError, UnicodeError):
        return None
    lines = text.splitlines()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            end = getattr(node, "end_lineno", node.lineno)
            return "\n".join(lines[node.lineno - 1 : end]), node.lineno
    return None


def invariant_findings(selected: set[str]) -> list[Finding]:
    """Validate cross-file contracts that a one-line regex cannot express."""
    findings: list[Finding] = []

    main_rel = "hermes_cli/main.py"
    if main_rel in selected:
        path = REPO_ROOT / main_rel
        cmd_gui = _function_source(path, "cmd_gui")
        if cmd_gui:
            source, line = cmd_gui
            route_at = source.find("_is_termux_startup_environment()")
            package_at = source.find("_desktop_packaged_executable(desktop_dir)")
            if route_at < 0 or package_at < 0 or route_at > package_at or "_cmd_termux_gui(args, desktop_dir)" not in source:
                findings.append(
                    Finding(
                        path,
                        line,
                        "def cmd_gui(...)",
                        "Termux route no longer exits before Electron packaging",
                        "Native Android cannot execute the glibc Electron package path.",
                        "Route `_is_termux_startup_environment()` to `_cmd_termux_gui(...)` before resolving/packaging Electron.",
                    )
                )
        termux_gui = _function_source(path, "_cmd_termux_gui")
        if termux_gui:
            source, line = termux_gui
            required = ('host="127.0.0.1"', 'os.environ["HERMES_WEB_DIST"]', "chromium_browser_spec")
            if any(token not in source for token in required) or 'host="0.0.0.0"' in source:
                findings.append(
                    Finding(
                        path,
                        line,
                        "def _cmd_termux_gui(...)",
                        "Termux Desktop loopback/X11 contract drift",
                        "The Termux Desktop host must serve the real Desktop dist on loopback and launch it through the X11 browser transport.",
                        "Keep HERMES_WEB_DIST=apps/desktop/dist, host=127.0.0.1, and chromium_browser_spec(runtime).",
                    )
                )

    package_rel = "apps/desktop/package.json"
    if package_rel in selected:
        path = REPO_ROOT / package_rel
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            command = str(payload.get("scripts", {}).get("build:renderer", ""))
        except (OSError, UnicodeError, json.JSONDecodeError):
            command = ""
        bad = (not command) or "vite build" not in command or any(
            token in command.lower() for token in ("electron", "node-pty", "stage-native")
        )
        if bad:
            findings.append(
                Finding(
                    path,
                    1,
                    'scripts["build:renderer"]',
                    "Termux renderer build depends on native Electron work",
                    "Termux must be able to build the latest Desktop renderer without running Electron/native lifecycle tooling.",
                    "Keep build:renderer limited to renderer prerequisites plus `vite build`.",
                )
            )

    req_rel = "scripts/termux_requirements.py"
    if req_rel in selected:
        path = REPO_ROOT / req_rel
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            source = ""
        required = ('"uvicorn": {"standard"}', '"python-telegram-bot": {"webhooks"}')
        if any(token not in source for token in required):
            findings.append(
                Finding(
                    path,
                    1,
                    "DROP_EXTRAS",
                    "Android-native optional dependency filter weakened",
                    "Desktop/server extras can pull unsupported native packages into the Termux resolver graph.",
                    "Retain the curated uvicorn[standard] and Telegram webhook-extra stripping in DROP_EXTRAS.",
                )
            )

    installer_rel = "scripts/install-termux.sh"
    if installer_rel in selected:
        path = REPO_ROOT / installer_rel
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            source = ""
        if not source.startswith("#!/data/data/com.termux/files/usr/bin/bash"):
            findings.append(
                Finding(
                    path,
                    1,
                    source.splitlines()[0] if source else "<missing>",
                    "native Termux installer lost its Termux shebang",
                    "The installer entrypoint must resolve the Termux bash, not a host/proot shell.",
                    "Restore #!/data/data/com.termux/files/usr/bin/bash.",
                )
            )
        if not re.search(r"unset(?:[^\n]*\\\n)*[^\n]*_PYTHON_HOST_PLATFORM", source):
            findings.append(
                Finding(
                    path,
                    1,
                    "_PYTHON_HOST_PLATFORM cleanup",
                    "native installer no longer scrubs fake Python host tags",
                    "Inherited _PYTHON_HOST_PLATFORM values can poison Android wheel selection.",
                    "Unset _PYTHON_HOST_PLATFORM before uv resolution/install.",
                )
            )

    x11_rel = "hermes_cli/termux_desktop.py"
    if x11_rel in selected:
        path = REPO_ROOT / x11_rel
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            source = ""
        required_x11_contract = (
            "https://github.com/termux/termux-x11/releases/download/",
            "https://api.github.com/repos/termux/termux-x11/releases/tags/nightly",
            "asset.get(\"digest\")",
            "_sha256_file(pending_apk)",
        )
        if any(token not in source for token in required_x11_contract):
            findings.append(
                Finding(
                    path,
                    1,
                    "TERMUX_X11_APK_URL",
                    "Termux:X11 APK origin/digest verification contract is incomplete",
                    "Auto-acquired Android application packages must come from the official Termux:X11 release and match GitHub's published SHA-256 digest.",
                    "Resolve the official nightly release metadata, require its digest, and verify the downloaded APK before opening Android's installer.",
                )
            )
    return findings


def _git_names(args: Sequence[str]) -> list[Path]:
    proc = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [REPO_ROOT / line for line in proc.stdout.splitlines() if line.strip()]


def staged_paths() -> list[Path]:
    return _git_names(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])


def diff_paths(base: str) -> list[Path]:
    return _git_names(["diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD"])


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--all", action="store_true", dest="all_files", help="scan the full repository")
    parser.add_argument("--diff", metavar="BASE", help="scan files changed from BASE...HEAD")
    parser.add_argument("--list", action="store_true", help="print files that will be scanned")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if sum(bool(x) for x in (args.paths, args.all_files, args.diff)) > 1:
        raise SystemExit("choose exactly one of explicit paths, --all, or --diff")

    if args.all_files:
        roots = [REPO_ROOT]
    elif args.diff:
        roots = diff_paths(args.diff)
    elif args.paths:
        roots = list(args.paths)
    else:
        roots = staged_paths()

    files = sorted(iter_files(roots), key=_repo_rel)
    if args.list:
        for path in files:
            print(_repo_rel(path))
        return 0

    selected = {_repo_rel(path) for path in files}
    findings = [finding for path in files for finding in scan_file(path)]
    findings.extend(invariant_findings(selected))
    findings.sort(key=lambda item: (_repo_rel(item.path), item.line, item.name))

    if not findings:
        print(f"Termux footgun check passed ({len(files)} file(s) scanned).")
        return 0

    print(f"Termux footgun check found {len(findings)} issue(s):", file=sys.stderr)
    for finding in findings:
        rel = _repo_rel(finding.path)
        print(f"\n{rel}:{finding.line}: {finding.name}", file=sys.stderr)
        print(f"  {finding.text.strip()}", file=sys.stderr)
        print(f"  Why: {finding.message}", file=sys.stderr)
        print(f"  Fix: {finding.fix}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
