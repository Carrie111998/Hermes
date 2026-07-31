"""Gateway lifecycle guard for cron job creation (#30719).

An agent running inside a gateway can schedule a cron job that calls
``hermes gateway restart`` (or ``launchctl kickstart ai.hermes.gateway``
or ``systemctl restart hermes-gateway``).  When the cron fires, the
gateway dies, the supervisor (launchd KeepAlive / systemd Restart=)
revives it, auto-resume picks up the offending session, and the resumed
turn re-runs the same logic — a SIGTERM-respawn loop every ~10 seconds
until manually broken.

This module rejects cron job specs whose prompt or script contains a
direct shell-level gateway-lifecycle command.  It is enforced at
``cron.jobs.create_job`` so it fires on every job-creation path: the
``hermes cron create`` CLI subcommand AND the agent's ``cronjob`` model
tool (which calls ``create_job`` directly, bypassing the CLI layer).

The pattern is intentionally command-shaped: it anchors on a concrete
command identifier (``hermes gateway``, ``launchctl ... hermes-gateway``,
``systemctl ... hermes-gateway``, ``pkill`` against the gateway) so it
cannot fire on prose.  A cron ``prompt`` is fed to a future LLM, not a
shell, so an over-broad substring match on English ("Kong API gateway
autoscaling and restart behavior") would produce a high false-positive
rate without preventing the actual foot-gun, which requires a real
command shape.

This is a defence-in-depth layer.  ``tools/terminal_tool.py`` already
blocks these commands at *execution* time when ``_HERMES_GATEWAY=1``, and
``hermes gateway stop|restart`` refuse to self-target from inside the
gateway.  Blocking at *creation* time as well means the agent gets an
immediate, informative rejection instead of scheduling a job that will
only fail (silently) when it fires.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Optional


class GatewayLifecycleBlocked(ValueError):
    """Raised when a cron job spec contains a gateway-lifecycle command."""


# Shell-level command shapes that target the gateway lifecycle. Each branch
# is anchored on a concrete command identifier so a match can only fire on
# actual shell-command-shaped strings, not on prose.
_GATEWAY_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    # Branch A: `hermes gateway restart|stop` — the canonical foot-gun.
    # `start` is intentionally excluded: starting a gateway from inside a
    # gateway is benign (a no-op or "already running" error), and a
    # legitimate cron job might start a sibling profile's gateway.
    r"(?:hermes\s+gateway\s+(?:restart|stop))"
    # Branch B: launchctl ops on a hermes-gateway label. macOS launchd
    # labels look like `ai.hermes.gateway` / `hermes-gateway`. Requiring the
    # gateway identifier prevents blocking unrelated hermes services (e.g.
    # `launchctl unload ai.hermes.update-checker.plist`).
    r"|(?:launchctl\s+(?:kickstart|unload|load|stop|restart)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch C: systemctl ops on a hermes-gateway unit.
    r"|(?:systemctl\s+(?:-\S+\s+)*(?:restart|stop|start)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch D: pkill / kill targeting the hermes gateway process. Both
    # token orders because real reproductions show both.
    r"|(?:p?kill\b[^\n]*\bhermes\b[^\n]*\bgateway)"
    r"|(?:p?kill\b[^\n]*\bgateway\b[^\n]*\bhermes)"
)

_COMMAND_SEPARATOR_CHARS = frozenset(";&|()\n")
_SHELLS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
_SHELL_COMMAND_PREFIXES = frozenset({
    "!",
    "{",
    "do",
    "elif",
    "else",
    "if",
    "then",
    "until",
    "while",
})
_ASSIGNMENT_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_QUOTED_ASSIGNMENT_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=([\"']).*")
_REDIRECTION_PATTERN = re.compile(r"\d*(?:<|>).*")
_ENV_OPTIONS_WITH_VALUES = frozenset({
    "-C",
    "-S",
    "-u",
    "--chdir",
    "--split-string",
    "--unset",
})
_COMMAND_WRAPPERS = frozenset({
    "arch",
    "caffeinate",
    "command",
    "doas",
    "env",
    "exec",
    "nice",
    "nohup",
    "setsid",
    "stdbuf",
    "sudo",
    "time",
    "timeout",
})
_SUDO_OPTIONS_WITH_VALUES = frozenset({
    "-C",
    "-D",
    "-g",
    "-h",
    "-p",
    "-R",
    "-T",
    "-u",
    "--chdir",
    "--chroot",
    "--close-from",
    "--command-timeout",
    "--group",
    "--host",
    "--prompt",
    "--role",
    "--type",
    "--user",
})
_SHELL_OPTIONS_WITH_VALUES = frozenset({"-O", "-o", "--init-file", "--rcfile"})
_OPTION_WRAPPERS = frozenset({
    "arch",
    "caffeinate",
    "doas",
    "nice",
    "nohup",
    "setsid",
    "stdbuf",
    "timeout",
})
_WRAPPER_OPTIONS_WITH_VALUES = {
    "caffeinate": frozenset({"-t", "-w"}),
    "doas": frozenset({"-C", "-u"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-e", "-i", "-o"}),
    "timeout": frozenset({"-k", "-s", "--kill-after", "--signal"}),
}


def contains_gateway_lifecycle_command(text: str) -> bool:
    """Return True if *text* contains a gateway lifecycle command pattern."""
    if not text:
        return False
    return bool(_GATEWAY_LIFECYCLE_PATTERN.search(text))


def _is_command_separator(token: str) -> bool:
    return bool(token) and not (set(token) - _COMMAND_SEPARATOR_CHARS)


def _skip_assignments(tokens: list[str], index: int) -> int:
    while index < len(tokens):
        token = tokens[index]
        quoted = _QUOTED_ASSIGNMENT_PATTERN.fullmatch(token)
        if quoted:
            quote = quoted.group(1)
            value = token.split("=", 1)[1]
            if len(value) >= 2 and value.endswith(quote):
                index += 1
                continue
            index += 1
            while index < len(tokens) and not tokens[index].endswith(quote):
                index += 1
            index += index < len(tokens)
            continue
        if _ASSIGNMENT_PATTERN.fullmatch(token):
            index += 1
            continue
        break
    return index


def _skip_redirections(tokens: list[str], index: int) -> int:
    while index < len(tokens) and _REDIRECTION_PATTERN.fullmatch(tokens[index]):
        token = tokens[index]
        if token.endswith(("<", ">")):
            if index + 2 < len(tokens) and tokens[index + 1] == "&":
                index += 3
            else:
                index += 2
        else:
            index += 1
        index = _skip_assignments(tokens, index)
    return index


def _shell_tokens(command: str) -> Optional[list[str]]:
    """Return a small, comment-aware shell token stream or ``None``."""
    if len(command) > 16_384:
        return None
    try:
        lexer = shlex.shlex(
            command.replace("\\\n", ""),
            posix=False,
            punctuation_chars=";&|()\n",
        )
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens: list[str] = []
        in_comment = False
        for token in lexer:
            if in_comment:
                if "\n" in token:
                    tokens.append("\n")
                    in_comment = False
                continue
            if token.startswith("#"):
                in_comment = True
                continue
            tokens.append(token)
        return tokens
    except ValueError:
        # Fail open on malformed shell text. Known cases are unbalanced quotes,
        # which the target shell also rejects rather than executing.
        return None


def _unquote(token: str) -> str:
    try:
        return shlex.split(token)[0]
    except (IndexError, ValueError):
        return token


def _contains_launchctl_submit_tokens(tokens: list[str], depth: int) -> bool:
    index = 0
    while index < len(tokens):
        if index and not _is_command_separator(tokens[index - 1]):
            index += 1
            continue
        index = _skip_assignments(tokens, index)
        while (
            index < len(tokens) and _unquote(tokens[index]) in _SHELL_COMMAND_PREFIXES
        ):
            index += 1
            index = _skip_assignments(tokens, index)
        index = _skip_redirections(tokens, index)
        while (
            index < len(tokens)
            and _unquote(tokens[index]).rsplit("/", 1)[-1] in _COMMAND_WRAPPERS
        ):
            wrapper = _unquote(tokens[index]).rsplit("/", 1)[-1]
            index += 1
            if wrapper == "command" and index < len(tokens) and tokens[index] == "-p":
                index += 1
            if wrapper == "env":
                while index < len(tokens):
                    assignment_end = _skip_assignments(tokens, index)
                    if assignment_end != index:
                        index = assignment_end
                        continue
                    if tokens[index] in _ENV_OPTIONS_WITH_VALUES:
                        index += 2
                        continue
                    if tokens[index].startswith("-") and tokens[index] != "--":
                        index += 1
                        continue
                    break
            if wrapper == "sudo":
                while index < len(tokens) and tokens[index].startswith("-"):
                    index += 2 if tokens[index] in _SUDO_OPTIONS_WITH_VALUES else 1
                index = _skip_assignments(tokens, index)
            if wrapper == "time" and index < len(tokens) and tokens[index] == "-p":
                index += 1
            if wrapper in _OPTION_WRAPPERS:
                options_with_values = _WRAPPER_OPTIONS_WITH_VALUES.get(
                    wrapper, frozenset()
                )
                while (
                    index < len(tokens)
                    and tokens[index].startswith("-")
                    and tokens[index] != "--"
                ):
                    index += 2 if tokens[index] in options_with_values else 1
                if wrapper == "timeout":
                    if index < len(tokens) and tokens[index] == "--":
                        index += 1
                    index += index < len(tokens)
            if index < len(tokens) and tokens[index] == "--":
                index += 1
        if index >= len(tokens) or _is_command_separator(tokens[index]):
            index += 1
            continue
        name = _unquote(tokens[index]).rsplit("/", 1)[-1]
        if (
            name == "launchctl"
            and index + 1 < len(tokens)
            and _unquote(tokens[index + 1]) == "submit"
        ):
            return True
        if name not in _SHELLS or depth >= 3:
            index += 1
            continue
        option_index = index + 1
        while option_index < len(tokens) - 1:
            option = _unquote(tokens[option_index])
            if _is_command_separator(option) or not option.startswith("-"):
                break
            if (
                option.startswith("-")
                and not option.startswith("--")
                and "c" in option[1:]
            ):
                payload_index = option_index + 1
                if (
                    payload_index < len(tokens)
                    and _unquote(tokens[payload_index]) == "--"
                ):
                    payload_index += 1
                if payload_index >= len(tokens):
                    break
                payload = _shell_tokens(_unquote(tokens[payload_index]))
                if payload and _contains_launchctl_submit_tokens(payload, depth + 1):
                    return True
                break
            option_index += 2 if option in _SHELL_OPTIONS_WITH_VALUES else 1
        index += 1
    return False


def contains_launchctl_submit_command(command: str) -> bool:
    """Detect an actual ``launchctl submit`` invocation in bounded shell text."""
    if len(command) > 16_384:
        return bool(
            re.search(r"(?i)\blaunchctl\s+submit\b", command.replace("\\\n", ""))
        )
    tokens = _shell_tokens(command)
    return bool(tokens and _contains_launchctl_submit_tokens(tokens, 0))


def _resolve_script_path(script_path: str) -> Path:
    """Resolve a cron ``script`` value the same way the scheduler does.

    The scheduler (``cron.scheduler``) resolves a bare/relative script path
    under ``<HERMES_HOME>/scripts/`` and only accepts absolute paths as-is.
    We MUST mirror that here so the guard scans the file that will actually
    run — otherwise a job whose script lives at the scheduler's real location
    (``~/.hermes/scripts/restart.sh``) but is passed as the bare name
    ``restart.sh`` would read as a nonexistent relative path and silently
    scan prompt-only content, letting the command through.
    """
    from hermes_constants import get_hermes_home

    raw = Path(script_path).expanduser()
    if raw.is_absolute():
        return raw
    return get_hermes_home() / "scripts" / raw


def _read_script_for_scanning(script_path: str) -> str:
    """Read a script file for lifecycle-pattern scanning.

    Decodes with ``errors="replace"`` so binary or non-UTF-8 content does not
    silently bypass the check — a plain text-mode read raises
    ``UnicodeDecodeError`` on such files, and swallowing that error would let
    an attacker hide the command in binary noise.  Returns an empty string
    only when the file cannot be read at all.
    """
    try:
        return _resolve_script_path(script_path).read_bytes().decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""


def check_gateway_lifecycle(
    prompt: Optional[str],
    script: Optional[str] = None,
) -> None:
    """Raise ``GatewayLifecycleBlocked`` if *prompt* or *script* contains a
    gateway-lifecycle command pattern.

    ``prompt`` is scanned directly.  ``script``, when supplied, is read from
    disk and concatenated for the scan.  Both are considered together so a
    job cannot slip through by splitting the command across the prompt and
    the script.

    Callers should let the exception propagate when they want the create to
    fail with a ``ValueError``-shaped error (the agent's ``cronjob`` tool
    surfaces this as a tool error; the CLI prints it in red and exits 1).
    """
    combined = prompt or ""
    if script:
        script_text = _read_script_for_scanning(script)
        if script_text:
            combined = f"{combined}\n{script_text}"

    if contains_gateway_lifecycle_command(combined):
        raise GatewayLifecycleBlocked(
            "Blocked: cron job contains a gateway lifecycle command "
            "(restart/stop/kill). This is blocked to prevent agent-driven "
            "SIGTERM-respawn loops under launchd/systemd supervision "
            "(#30719). Run `hermes gateway restart` from a shell outside "
            "the running gateway instead."
        )
