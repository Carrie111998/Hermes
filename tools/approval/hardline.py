"""Hardline command blocking: unconditional deny rules and pattern definitions.

Contains the rm/system hardline floor (HARDLINE_PATTERNS), the user-config
deny glob matcher, blocked-payload persistence, the DANGEROUS_PATTERNS table
used by the general dangerous-command detector, and command normalization
helpers shared with the detector.
"""

import fnmatch
import logging
import os
import re
import unicodedata
from typing import Optional

from tools.approval.context import (
    _CMDPOS,
    _COMMAND_TAIL,
    _HERMES_CONFIG_PATH,
    _HERMES_ENV_PATH,
    _PROJECT_SENSITIVE_WRITE_TARGET,
    _SENSITIVE_WRITE_TARGET,
    _SYSTEM_CONFIG_PATH,
    _USER_SENSITIVE_WRITE_TARGET,
    _WRITE_TARGET_BOUNDARY,
)
from tools.approval.shell_parser import (
    _MALFORMED_EXEC_DESCRIPTION,
    _PARSER_LIMIT_DESCRIPTION,
    _command_detection_variants,
    _command_parser_limit_exceeded,
    _grep_safe_detection_variant,
    _rewrite_resolved_hermes_home,
    _rewrite_resolved_user_home,
)

logger = logging.getLogger(__name__)

# Late-bound package access: tests monkeypatch these attributes on tools.approval
# and internal calls must observe the patches at call time.
import tools.approval as _approval_pkg  # noqa: E402

def _hardline_rm_path(path_alt: str, tail: str = r'(?:\s|$|[)`;|&])') -> str:
    return rf'(?:["\'](?:{path_alt})["\']|(?:{path_alt}){tail})'


# Protected system roots whose recursive deletion has no recovery path.
_HARDLINE_SYSTEM_DIRS = (
    r'/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|'
    r'/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*'
)

# `rm` plus its flag group, shared by the three rm hardline rules. Kept as a
# plain concatenation (not an f-string) so the regex backslashes never live
# inside an f-string replacement field — unsupported on the Python 3.11 floor.
#
# Anchored to _CMDPOS (start of line, after a command separator ; && || |,
# after a subshell opener $(/backtick, or after sudo/env/exec wrappers) so the
# rule fires only when `rm` is an actual command word — not when the literal
# string "rm -rf /" appears as DATA inside another command's argument, e.g.
# `gh pr create --title "block rm -rf / spellings"` or `git commit -m "…rm -rf
# /…"`. Those tripped the unconditional floor and could not run at all before
# the anchor. A real wipe at any command position (bare, chained, in $()/`…`,
# under sudo) still matches; the quoted-path branch in _hardline_rm_path keeps
# catching `rm -rf "/"`.
_RM_FLAG_PREFIX = _CMDPOS + r'rm\s+(-[^\s]*\s+)*'

HARDLINE_PATTERNS = [
    # rm recursive targeting the root filesystem or protected roots.
    # `${HOME}` brace form and quoted paths (`rm -rf "/"`, `rm -rf "$HOME"`)
    # are handled via _hardline_rm_path so the floor cannot be bypassed with
    # the ordinary quoting/brace shell idioms.
    #
    # The path token matches any root-anchored path whose components collapse
    # back to "/" in the shell: a bare "/", repeated slashes ("//"), and
    # "."/".." current/parent segments ("/.", "/./", "/..", "/../..") all
    # resolve to root, optionally followed by a trailing glob ("/*", "//*").
    # Each inter-slash segment must be exactly "." or "..", so a longer dot
    # run or any real name is a literal directory, NOT root — "/tmp", "/home",
    # "/.ssh", "/.config" and even "/..." (a dir literally named "...") fall
    # through to the softer DANGEROUS_PATTERNS / system-directory rules
    # instead of being unconditionally hardline-blocked. The explicit "/ \*"
    # alt preserves the slash-space-glob spelling (`rm -rf / *`, which the
    # shell sees as two args: "/" plus the "*" glob).
    (_RM_FLAG_PREFIX + _hardline_rm_path(r'/(?:(?:\.\.?)?/)*(?:\.\.?)?\**|/ \*'), "recursive delete of root filesystem"),
    (_RM_FLAG_PREFIX + _hardline_rm_path(_HARDLINE_SYSTEM_DIRS), "recursive delete of system directory"),
    (_RM_FLAG_PREFIX + _hardline_rm_path(r'(?:~|\$\{?HOME\}?)(?:/?|/\*)?'), "recursive delete of home directory"),
    # Filesystem format
    (r'\bmkfs(\.[a-z0-9]+)?\b', "format filesystem (mkfs)"),
    # Raw block device overwrites (dd + redirection)
    (r'\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*', "dd to raw block device"),
    (r'>\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b', "redirect to raw block device"),
    # Fork bomb (classic shell form)
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    # Kill every process on the system
    (r'\bkill\s+(-[^\s]+\s+)*-1\b', "kill all processes"),
    # System shutdown / reboot — anchor to command position (start of line,
    # after a command separator, or after sudo/env wrappers) so we don't
    # false-positive on "echo reboot" or "grep 'shutdown' logs".
    # _CMDPOS matches start-of-command positions.
    (_CMDPOS + r'(shutdown|reboot|halt|poweroff)\b', "system shutdown/reboot"),
    (_CMDPOS + r'init\s+[06]\b', "init 0/6 (shutdown/reboot)"),
    (_CMDPOS + r'systemctl\s+(poweroff|reboot|halt|kexec)\b', "systemctl poweroff/reboot"),
    (_CMDPOS + r'telinit\s+[06]\b', "telinit 0/6 (shutdown/reboot)"),
]

# Pre-compiled variant used by the hot-path matcher. Building these at module
# load eliminates the ~2.6 ms cold-cache re.compile fan-out on the first
# terminal() call per process (12 HARDLINE + 47 DANGEROUS patterns, each
# potentially evicted from Python's 512-entry ``re._cache`` by unrelated
# regex work elsewhere in the agent). DANGEROUS_PATTERNS_COMPILED is built
# at the end of this module after DANGEROUS_PATTERNS is defined.
_RE_FLAGS = re.IGNORECASE | re.DOTALL
HARDLINE_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in HARDLINE_PATTERNS
]


# =========================================================================
# Sudo stdin guard — block password guessing via "sudo -S"
# =========================================================================
# When SUDO_PASSWORD is not configured, any explicit "sudo -S" in the
# command is the LLM piping a guessed password via stdin.  This is a
# brute-force attack vector: the model iterates through candidate
# passwords, inspects sudo's "Sorry, try again" output, and refines.
# Treat this as an unconditional block — there is never a legitimate
# reason for the agent to pipe passwords to sudo -S when no password
# has been configured.
_SUDO_STDIN_RE = re.compile(
    r'(?:^|[;&|`\n]|&&|\|\||\$\()\s*sudo\s+-S\b',
    re.IGNORECASE)


def _check_sudo_stdin_guard(command: str) -> tuple:
    """Detect ``sudo -S`` (stdin password) without configured SUDO_PASSWORD.

    When SUDO_PASSWORD is set, ``_transform_sudo_command`` injects ``-S``
    internally — that path is legitimate and handled elsewhere.  This guard
    only fires when SUDO_PASSWORD is *not* set, meaning the LLM explicitly
    wrote ``sudo -S`` to pipe a guessed password.

    Returns:
        (is_blocked: bool, description: str | None)
    """
    if "SUDO_PASSWORD" in os.environ:
        return (False, None)
    normalized = _normalize_command_for_detection(command).lower()
    if _SUDO_STDIN_RE.search(normalized):
        return (True, "sudo password guessing via stdin (sudo -S)")
    return (False, None)


def detect_hardline_command(command: str) -> tuple:
    """Check if a command matches hardline blocklist patterns.

    Hardline patterns are NEVER bypassable, even in YOLO mode.

    Returns:
        (is_hardline, description) or (False, None)
    """
    if _command_parser_limit_exceeded(command):
        return (True, _PARSER_LIMIT_DESCRIPTION)
    normalized = _normalize_command_for_detection(command)
    _, malformed_grep = _grep_safe_detection_variant(normalized)
    if malformed_grep:
        return (True, _MALFORMED_EXEC_DESCRIPTION)
    for command_variant in _command_detection_variants(command):
        variant_lower = command_variant.lower()
        for pattern_re, description in HARDLINE_PATTERNS_COMPILED:
            if pattern_re.search(variant_lower):
                return (True, description)
    return (False, None)


def _match_user_deny_rule(command: str) -> str | None:
    # Imported lazily to break the hardline<->gate import cycle.
    """Return the matching ``approvals.deny`` glob, or None.

    ``approvals.deny`` in config.yaml is a user-defined list of fnmatch
    globs that block a command unconditionally — like the hardline floor,
    a deny match fires BEFORE the yolo / mode=off bypass. It is the
    user-editable counterpart to the code-shipped hardline blocklist:
    "never let the agent run this, even under yolo".

    Matching is case-insensitive and runs over the same normalized /
    deobfuscated command variants the dangerous-pattern detector uses, so
    quoting tricks (``r\\m``, ``git st""atus``) can't sidestep a rule any
    more easily than they sidestep detection. Empty/absent list = no-op.
    """
    from tools.approval import _get_approval_config
    try:
        deny_patterns = _get_approval_config().get("deny") or []
    except Exception:
        return None
    if not deny_patterns:
        return None
    globs = [p.strip() for p in deny_patterns
             if isinstance(p, str) and p.strip()]
    if not globs:
        return None
    for command_variant in _command_detection_variants(command):
        candidate = command_variant.lower().strip()
        for pattern in globs:
            if fnmatch.fnmatchcase(candidate, pattern.lower()):
                return pattern
    return None


def _user_deny_block_result(pattern: str) -> dict:
    """Build the standard block result for an ``approvals.deny`` match."""
    return {
        "approved": False,
        "user_deny": True,
        "message": (
            f"BLOCKED: this command matches the user-defined deny rule "
            f"'{pattern}' (approvals.deny in config.yaml). It cannot be "
            "executed via the agent — not even with --yolo, /yolo, or "
            "approvals.mode=off. Do NOT retry or rephrase this command; "
            "the user has explicitly forbidden it."
        ),
    }


def _save_blocked_payload(command: str) -> Optional[str]:
    """Persist a parser-limit-blocked command as a runnable script.

    The parser-limit block fires on payload SIZE/shape, not on the
    operation — the command itself is usually a legitimate script the
    model inlined (heredoc, giant one-liner). Materialize it to a file so
    the recovery is one turn (`bash <file>`) instead of two (re-author via
    write_file, then run). Saving is strictly safer than the hint-only
    path: the file goes through the same execution pipeline as any other
    script (including the referenced-script content guard), and nothing
    is executed here.

    Returns the saved path, or None on any failure (the hint then falls
    back to the manual write_file recipe).
    """
    try:
        from hermes_constants import get_hermes_home
        import time as _time
        import uuid as _uuid
        script_dir = get_hermes_home() / "cache" / "blocked-scripts"
        script_dir.mkdir(parents=True, exist_ok=True)
        # Opportunistic cleanup: blocked payloads older than 7 days.
        cutoff = _time.time() - 7 * 86400
        for old in script_dir.glob("blocked-*.sh"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        path = script_dir / f"blocked-{int(_time.time())}-{_uuid.uuid4().hex[:8]}.sh"
        path.write_text(
            "#!/bin/bash\n"
            "# Auto-saved by Hermes: this command exceeded the inline command\n"
            "# parser limit and was blocked from direct execution. Review it,\n"
            "# then run it via: bash " + str(path) + "\n"
            + command
            + ("\n" if not command.endswith("\n") else ""),
            encoding="utf-8", errors="replace",
        )
        return str(path)
    except Exception:
        logger.debug("failed to save blocked payload", exc_info=True)
        return None


def _hardline_block_result(description: str, command: str = "") -> dict:
    """Build the standard block result for a hardline match."""
    message = (
        f"BLOCKED (hardline): {description}. "
        "This command is on the unconditional blocklist and cannot "
        "be executed via the agent — not even with --yolo, /yolo, "
        "approvals.mode=off, or cron approve mode. If you genuinely "
        "need to run it, run it yourself in a terminal outside the "
        "agent."
    )
    # The parser-limit block is almost always a giant inline payload
    # (heredoc script, base64 blob, one-line python -c program) — not a
    # genuinely forbidden operation. 198 occurrences in a 250k-call
    # production window, typically followed by blind rephrase retries.
    # Auto-save the payload as a runnable script and point at it; fall
    # back to the manual write_file recipe when saving fails.
    if description in (_PARSER_LIMIT_DESCRIPTION, _MALFORMED_EXEC_DESCRIPTION):
        saved = _approval_pkg._save_blocked_payload(command) if command else None
        if saved:
            message += (
                " RECOVERY: this block fires on oversized/unparseable inline "
                "command payloads (heredocs, giant one-liners), not on the "
                f"operation itself. Your command was saved to {saved} — "
                f"review it, then run: terminal(command=\"bash {saved}\"). "
                "Do not retry inline."
            )
        else:
            message += (
                " RECOVERY: this block fires on oversized/unparseable inline "
                "command payloads (heredocs, giant one-liners), not on the "
                "operation itself. Write the script to a file with write_file, "
                "then run it: terminal(command=\"bash /path/script.sh\") or "
                "\"python3 /path/script.py\". Do not retry inline."
            )
    return {
        "approved": False,
        "hardline": True,
        "message": message,
    }


def _sudo_stdin_block_result(description: str) -> dict:
    """Build the standard block result for sudo stdin guard."""
    return {
        "approved": False,
        "message": (
            f"BLOCKED: {description}. "
            "Do not pipe passwords to 'sudo -S' — this is a brute-force "
            "attack vector. Set SUDO_PASSWORD in your .env file if the "
            "agent needs passwordless sudo, or run the sudo command "
            "manually in your own terminal."
        ),
    }


# =========================================================================
# Dangerous command patterns
# =========================================================================

DANGEROUS_PATTERNS = [
    (r'\brm\s+(-[^\s]*\s+)*/', "delete in root path"),
    (r'\brm\s+-[^\s]*r', "recursive delete"),
    (r'\brm\s+--recursive\b', "recursive delete (long flag)"),
    # GNU rm permutes options, so a recursive flag group may legally FOLLOW
    # the operands: `rm build/ -rf`, `rm build/ -r -f`, and `rm build/
    # --recursive --force` are all equivalent to the flags-first spellings the
    # two patterns above catch — without this rule they run with no approval
    # prompt at all. The operand run is tempered: it cannot cross a command
    # separator (`;`, `|`, `&`, newline — so a later pipeline segment's flags,
    # e.g. `rm foo | grep -r bar`, are not attributed to `rm`), cannot cross a
    # quote (so `git commit -m "rm x" --amend` style data can't bridge an `rm`
    # word to an unrelated dash token), and cannot cross a bare ` -- `
    # end-of-options separator (after `--`, POSIX rm treats `-rf` as a literal
    # filename, not flags; guarded both leading and mid-run). The flag token
    # itself must start right after whitespace so the `r` inside long options
    # like `--registry` (preceded by `-`, not whitespace) does not count.
    # Port of openai/codex#33464 ("recognize force options when they follow
    # operands").
    (r'\brm\s+(?!--(?:\s|$))(?:(?!\s--(?:\s|$))[^\n"\';|&])*\s'
     r'(?:-[a-z]*r[a-z]*\b|--recursive\b)',
     "recursive delete (flags after operands)"),
    # Windows shell front-ends have destructive built-ins that do not look like
    # Unix `rm`. Gate only when they are executed through cmd/powershell so
    # ordinary prose or filenames containing "del"/"rd" do not trip the guard.
    (r'\bcmd(?:\.exe)?\s+/(?:c|k)\s+.*\b(?:del|erase|rd|rmdir)\b', "Windows cmd destructive delete"),
    # PowerShell/pwsh: the destructive verb runs as the default positional
    # argument, so `powershell Remove-Item ...` needs NO explicit -Command.
    # Anchor the verb to the command position (right after the shell name,
    # after any leading `-Flag` switches, and optionally after -Command/-c)
    # so bare invocations are caught while a benign path arg containing
    # "del"/"rm" (e.g. `-File c:\del-logs\run.ps1`) is not.
    (r'\b(?:powershell|pwsh)(?:\.exe)?\b(?:\s+-\S+)*\s+(?:-(?:command|c)\s+)?["\']?(?:remove-item|rmdir|erase|del|rd|ri|rm)\b', "Windows PowerShell destructive delete"),
    (r'\b(?:powershell|pwsh)(?:\.exe)?\b.*\s-(?:encodedcommand|enc|e)\b', "PowerShell encoded command execution"),
    (r'\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b', "world/other-writable permissions"),
    (r'\bchmod\s+--recursive\b.*(777|666|o\+[rwx]*w|a\+[rwx]*w)', "recursive world/other-writable (long flag)"),
    (r'\bchown\s+(-[^\s]*)?R\s+root', "recursive chown to root"),
    (r'\bchown\s+--recur[a-z]*\b.*root', "recursive chown to root (long flag)"),
    (r'\bmkfs\b', "format filesystem"),
    (r'\bdd\s+.*if=', "disk copy"),
    (r'>\s*/dev/sd', "write to block device"),
    (r'\bDROP\s+(TABLE|DATABASE)\b', "SQL DROP"),
    # Use [^\n]* instead of .* so DOTALL mode does not cause a WHERE clause on the
    # *next* line to satisfy the negative lookahead, silently allowing DELETE without WHERE.
    (r'\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)', "SQL DELETE without WHERE"),
    (r'\bTRUNCATE\s+(TABLE)?\s*\w', "SQL TRUNCATE"),
    (rf'>\s*{_SYSTEM_CONFIG_PATH}', "overwrite system config"),
    (r'\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b', "stop/restart system service"),
    (r'\bkill\s+-9\s+-1\b', "kill all processes"),
    (r'\bpkill\s+-9\b', "force kill processes"),
    # killall with SIGKILL (parallel to pkill -9). Catches -9 / -KILL /
    # -s KILL / -SIGKILL forms, and also `killall -r <regex>` broad sweeps
    # that can wipe out unrelated processes by accident.
    # Inspired by Claude Code 2.1.113 expanded deny rules.
    (r'\bkillall\s+(-[^\s]*\s+)*-(9|KILL|SIGKILL)\b', "force kill processes (killall -KILL)"),
    (r'\bkillall\s+(-[^\s]*\s+)*-s\s+(KILL|SIGKILL|9)\b', "force kill processes (killall -s KILL)"),
    (r'\bkillall\s+(-[^\s]*\s+)*-r\b', "kill processes by regex (killall -r)"),
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    # Shell -c is parsed structurally by _execution_flag_findings(). A regex
    # that merely searched a dash-token for "c" also matched --norc,
    # --rcfile, and --restricted.
    (r'\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)', "pipe remote content to shell"),
    (r'\b(bash|sh|zsh|ksh)\s+<\s*<?\s*\(\s*(curl|wget)\b', "execute remote script via process substitution"),
    # Remote content executed via command substitution: eval/source/. $(curl ...)
    # or `wget ...`. Equivalent to piping remote content to a shell.
    (r'(?:\beval\b|\bsource\b|\.)\s*(?:\$\(\s*|`\s*)(?:curl|wget)\b', "execute remote content via command substitution"),
    # Decode-and-execute: encoded/transformed content piped to a shell. Without
    # these, `echo <base64> | base64 -d | bash` silently runs `rm -rf /` or any
    # other command because the raw text carries no dangerous keywords.
    (r'\b(base64|base32|base16)\s+(?:-[dD]|--decode)\b.*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe decoded content to shell (possible command obfuscation)"),
    # xxd reverse hex dump to shell (xxd uses -r for decode, not -d).
    (r'\bxxd\s+-r\b.*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe xxd-decoded content to shell (possible command obfuscation)"),
    # Character transformation via tr piped to shell:
    # `echo 'eq -pe v/' | tr 'eqv' 'rmf' | bash` decodes to `rm -rf /`.
    (r'\becho\b[^|]*\|\s*\btr\b[^|]*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe tr-transformed output to shell (possible command obfuscation)"),
    # openssl decode piped to shell:
    # `echo <base64> | openssl base64 -d | bash` decodes arbitrary commands.
    (r'\bopenssl\b.*\b(?:base64|enc)\b[^|]*\s+-[dD]\b[^|]*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe openssl-decoded content to shell (possible command obfuscation)"),
    (rf'\btee\b.*["\']?{_SENSITIVE_WRITE_TARGET}', "overwrite system file via tee"),
    (rf'>>?\s*["\']?{_SENSITIVE_WRITE_TARGET}', "overwrite system file via redirection"),
    (rf'\btee\b.*["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_WRITE_TARGET_BOUNDARY}', "overwrite project env/config via tee"),
    (rf'>>?\s*["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_WRITE_TARGET_BOUNDARY}', "overwrite project env/config via redirection"),
    (r'\bxargs\s+.*\brm\b', "xargs with rm"),
    # find -exec rm / -execdir rm — the -execdir variant (same semantics,
    # runs in the directory of each match) was previously missed. Claude
    # Code 2.1.113 tightened their equivalent find rule to stop auto-
    # approving -exec / -delete flags.
    (r'\bfind\b.*-exec(?:dir)?\s+(/\S*/)?rm\b', "find -exec/-execdir rm"),
    (r'\bfind\b.*-delete\b', "find -delete"),
    # Gateway lifecycle protection: prevent the agent from killing its own
    # gateway process.  These commands trigger a gateway restart/stop that
    # terminates all running agents mid-work.  Allow global flags between
    # `hermes` and `gateway` (e.g. `hermes -p ade gateway restart`) so a
    # profile flag can't slip the agent past the guard.
    (r'\bhermes\s+(?:-{1,2}\S+(?:\s+\S+)?\s+)*gateway\s+(stop|restart)\b', "stop/restart hermes gateway (kills running agents)"),
    (r'\bhermes\s+update\b', "hermes update (restarts gateway, kills running agents)"),
    # Docker container lifecycle — any user with docker.sock mounted (a common
    # Docker Compose pattern) gives the agent the ability to restart/stop/kill
    # containers without approval.  These are agent-initiated lifecycle operations
    # that should always require user consent, just like `hermes gateway restart`
    # already does for the gateway process.
    # Docker/Podman daemon redirect — global flags or env prefixes that point
    # the CLI at a DIFFERENT daemon, often a remote host over ssh/tcp.  A
    # command that looks local (`docker -H ssh://prod stop app`) silently
    # operates on remote infrastructure, so any docker/podman invocation
    # carrying a redirect requires approval regardless of subcommand.  The
    # redirect flag must appear in the global-flag position (before the
    # subcommand) and -H/--host/--context must carry a value, which keeps
    # `docker -h` (help) and subcommand flags like `docker run -h <hostname>`
    # out of the deny.  Listed BEFORE the lifecycle rules so a redirected
    # lifecycle command surfaces the more specific "remote daemon" reason.
    # Inspired by Claude Code 2.1.214, which added permission prompts for
    # docker/podman commands carrying daemon-redirect flags (--url,
    # --connection, --identity, remote mode).
    (r'\bdocker\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(?:-h|--host)[=\s]+\S+',
     "docker with remote daemon redirect (-H/--host)"),
    (r'\bdocker\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(?:-c|--context)[=\s]+\S+',
     "docker with daemon redirect (--context: alternate daemon)"),
    (r'\bdocker\s+context\s+use\b',
     "docker context use (switches default daemon for future commands)"),
    (r'\bpodman\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(?:--url|--connection|--identity)[=\s]+\S+',
     "podman with remote daemon redirect (--url/--connection/--identity)"),
    (r'\bpodman\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(?:-r\b|--remote\b)',
     "podman remote mode (-r/--remote: remote daemon)"),
    (r'\b(?:docker_host|docker_context|container_host|container_connection)=\S+',
     "docker/podman daemon redirect via environment (DOCKER_HOST/CONTAINER_HOST)"),
    # Allow global flags between `docker`/`compose` and the verb (e.g.
    # `docker compose -f prod.yml down`, `docker --log-level debug stop app`)
    # and the legacy hyphenated `docker-compose` binary, so a flag can't slip
    # a lifecycle command past the guard — same treatment as the `hermes ...
    # gateway` pattern above.
    (r'\bdocker(?:-compose|\s+compose)\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(restart|stop|kill|down)\b',
     "docker compose restart/stop/kill/down (container lifecycle)"),
    (r'\bdocker\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(restart|stop|kill)\b',
     "docker restart/stop/kill (container lifecycle)"),
    # Gateway protection: never start gateway outside systemd management
    (r'gateway\s+run\b.*(&\s*$|&\s*;|\bdisown\b|\bsetsid\b)', "start gateway outside systemd (use 'systemctl --user restart hermes-gateway')"),
    (r'\bnohup\b.*gateway\s+run\b', "start gateway outside systemd (use 'systemctl --user restart hermes-gateway')"),
    # Self-termination protection: prevent agent from killing its own process
    (r'\b(pkill|killall)\b.*\b(hermes|gateway|cli\.py)\b', "kill hermes/gateway process (self-termination)"),
    # Self-termination via kill + command substitution (pgrep/pidof).
    # The name-based pattern above catches `pkill hermes` but not
    # `kill -9 $(pgrep -f hermes)` because the substitution is opaque
    # to regex at detection time. Catch the structural pattern instead.
    # `pidof` is the BSD/Linux alternative to `pgrep` and is equally
    # opaque, so include it in the same alternation.
    (r'\bkill\b.*\$\(\s*(pgrep|pidof)\b', "kill process via pgrep/pidof expansion (self-termination)"),
    (r'\bkill\b.*`\s*(pgrep|pidof)\b', "kill process via backtick pgrep/pidof expansion (self-termination)"),
    # launchctl-driven gateway stop/restart on macOS. The agent can bypass
    # the `hermes gateway stop|restart` pattern above by driving launchd
    # directly against the service label (commonly `ai.hermes.gateway`).
    # Catch the operations that stop, restart, or unload it.
    (r'\blaunchctl\s+(stop|kickstart|bootout|unload|kill|disable|remove)\b.*\b(hermes|ai\.hermes)\b', "stop/restart hermes launchd service (kills running agents)"),
    # File copy/move/edit into sensitive system paths (/etc/ and macOS
    # /private/etc/ mirror).
    (rf'\b(cp|mv|install)\b.*\s{_SYSTEM_CONFIG_PATH}', "copy/move file into system config path"),
    (rf'\b(cp|mv|install)\b.*\s["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_COMMAND_TAIL}', "overwrite project env/config file"),
    # cp/mv/install OVERWRITING a sensitive credential/SSH/shell-rc/Hermes file.
    # The tee/redirection patterns above already gate _SENSITIVE_WRITE_TARGET
    # (~/.ssh/*, ~/.netrc/.pgpass/.npmrc/.pypirc, shell rc files,
    # ~/.hermes/config.yaml/.env), but cp/mv/install was only paired for /etc and
    # project-relative env/config — so `cp evil ~/.ssh/authorized_keys` (key
    # implant), `cp creds ~/.netrc`, and `cp evil ~/.bashrc` (login-time command
    # injection) slipped through with auto-approve. Same unpaired-door rationale
    # as #14639 / the sed-tee-redirect pairing on these targets.
    # Anchor the sensitive target to the command tail so this fires on the
    # DESTINATION (last arg) only — `cp evil ~/.ssh/authorized_keys` is gated,
    # but reading OUT of a sensitive path (`cp ~/.ssh/config /tmp/x`) stays safe.
    # The trailing `[^\s"\']*` consumes the rest of the destination filename
    # (e.g. `authorized_keys` after the `~/.ssh/` fragment).
    (rf'\b(cp|mv|install)\b.*\s["\']?{_SENSITIVE_WRITE_TARGET}[^\s"\']*["\']?{_COMMAND_TAIL}', "copy/move file into sensitive credential/SSH/shell-rc path"),
    # In-place edits mutate the target file directly, bypassing redirection,
    # tee, and copy/move/install coverage. Gate the same user-controlled
    # startup/credential files so `sed -i ... ~/.bashrc` and `perl -i ...
    # ~/.ssh/authorized_keys` cannot silently plant login commands or keys.
    (rf'\bsed\s+-[^\s]*i.*(?:{_USER_SENSITIVE_WRITE_TARGET})[^\s"\']*', "in-place edit of sensitive credential/SSH/shell-rc path"),
    (rf'\bsed\s+--in-place\b.*(?:{_USER_SENSITIVE_WRITE_TARGET})[^\s"\']*', "in-place edit of sensitive credential/SSH/shell-rc path (long flag)"),
    (rf'\b(?:perl|ruby)\b.*(?:^|\s)-[^\s]*i\b.*(?:{_USER_SENSITIVE_WRITE_TARGET})[^\s"\']*', "in-place edit of sensitive credential/SSH/shell-rc path (perl/ruby)"),
    (rf'\bsed\s+-[^\s]*i.*\s{_SYSTEM_CONFIG_PATH}', "in-place edit of system config"),
    (rf'\bsed\s+--in-place\b.*\s{_SYSTEM_CONFIG_PATH}', "in-place edit of system config (long flag)"),
    # In-place edit of a Hermes-managed security file (~/.hermes/config.yaml or
    # .env). sed -i bypasses the redirection/tee patterns above because it
    # mutates the file directly. Pairs the file_tools write_file/patch deny so
    # the terminal side is not an open door. See #14639.
    (rf'\bsed\s+-[^\s]*i.*(?:{_HERMES_CONFIG_PATH}|{_HERMES_ENV_PATH})', "in-place edit of Hermes config/env"),
    (rf'\bsed\s+--in-place\b.*(?:{_HERMES_CONFIG_PATH}|{_HERMES_ENV_PATH})', "in-place edit of Hermes config/env (long flag)"),
    # perl -i and ruby -i perform the same in-place mutation as sed -i but are
    # not caught by the -e/-c script-execution pattern above (which targets code
    # evaluation, not file mutation). Pairs the sed -i coverage from #14639.
    # The -i flag can appear as its own token after other flags
    # (`perl -p -i -e ... config.yaml`), combined (`perl -pi -e`), or with a
    # backup suffix (`perl -i.bak`). Match any flag token containing `i`
    # anywhere in the args, not just the first token — `perl -e '...'` (code
    # eval, no -i) does not trip because it has no `-...i` flag token.
    (rf'\b(?:perl|ruby)\b.*(?:^|\s)-[^\s]*i\b.*(?:{_HERMES_CONFIG_PATH}|{_HERMES_ENV_PATH})', "in-place edit of Hermes config/env (perl/ruby)"),
    # Interpreter heredocs are handled by _execution_flag_findings() alongside
    # inline-exec flags; keep only shell heredocs regex-based here.
    # Shell execution via heredoc — `bash <<'EOF' ... EOF` runs arbitrary
    # shell commands without triggering the `bash -c` pattern above. The
    # inner commands may not individually match any dangerous pattern (e.g.
    # data-exfiltration pipelines using curl/cat) yet are still executed in
    # a full shell context.
    (r'\b(bash|sh|zsh|ksh)\s+<<', "shell execution via heredoc"),
    # Git destructive operations that can lose uncommitted work or rewrite
    # shared history. Not captured by rm/chmod/etc patterns.
    # `git reset --hard` accepts any unambiguous long-flag prefix (--h,
    # --ha, --har, --hard) because git's own option parser resolves
    # abbreviated long flags -- `--hard` is the only `git reset` mode
    # starting with "h" (siblings are --soft/--mixed/--merge/--keep), so
    # this cannot collide with another reset mode. It also does not match
    # `--help`, which git special-cases before mode resolution.
    (r'\bgit\s+reset\s+--h(?:a(?:r(?:d)?)?)?\b', "git reset --hard (destroys uncommitted changes)"),
    (r'\bgit\s+push\b.*--forc[a-z]*\b', "git force push (rewrites remote history)"),
    (r'\bgit\s+push\b.*-f\b', "git force push short flag (rewrites remote history)"),
    (r'\bgit\s+clean\s+-[^\s]*f', "git clean with force (deletes untracked files)"),
    (r'\bgit\s+branch\s+-D\b', "git branch force delete"),
    # `-D` is shorthand for `-d --force`; the long-flag spellings
    # (`--delete`, `--force`) are different tokens entirely, so they slip
    # past the `-D\b` pattern above even though `git branch -d --force`
    # and `git branch --delete --force` delete an unmerged branch exactly
    # like `-D` does. Match delete+force in either order, bounded to the
    # same command segment (not spanning `;`/`|`/`&`/newline) the same
    # way the sudo patterns below do, to avoid contaminating an unrelated
    # later command in the same script.
    (r'\bgit\s+branch\b[^;|&\n]*?(?:-d\b|--delete\b)[^;|&\n]*?(?:-f\b|--force\b)', "git branch force delete (long flags)"),
    (r'\bgit\s+branch\b[^;|&\n]*?(?:-f\b|--force\b)[^;|&\n]*?(?:-d\b|--delete\b)', "git branch force delete (long flags, force-first)"),
    # Script execution after chmod +x — catches the two-step pattern where
    # a script is first made executable then immediately run. The script
    # content may contain dangerous commands that individual patterns miss.
    (r'\bchmod\s+\+x\b.*[;&|]+\s*\./', "chmod +x followed by immediate execution"),
    # Sudo with stdin / askpass / shell / list-privs flags. An LLM-driven
    # agent has no TTY, so sudo invocations that succeed without human
    # interaction are those reading the password from stdin (-S/--stdin)
    # or via an askpass helper (-A/--askpass). The shell-launch (-s) and
    # list-privileges (-a) flags are also gated since they are
    # privilege-relevant invocations the agent can chain after acquiring
    # the password (e.g. read SUDO_PASSWORD from .env -> sudo -S -s ->
    # root shell). Plain `sudo cmd` (no flag) is TTY-bound and excluded.
    # `_normalize_command_for_detection` lowercases input before pattern
    # matching, so case variants of S/s and A/a collapse — both forms
    # are gated below. Lazy `[^;|&\n]*?` allows flag arguments (e.g.
    # `sudo -u root -S whoami`) without spanning command separators. See
    # #17873 category 4.
    # sudo's own option parser (like git's) resolves unambiguous
    # long-flag prefixes, so `sudo --stdi` runs identically to
    # `sudo --stdin` and `sudo --ask` to `sudo --askpass` -- confirmed
    # against a live sudo binary. `--st[a-z]*` and `--a[a-z]*` are safe
    # to match broadly: per `man sudo`, `--stdin` is the only long option
    # starting with "st" (siblings are --shell/--set-home) and
    # `--askpass` is the only one starting with "a" at all.
    (r'\bsudo\b[^;|&\n]*?\s+(?:-s\b|--st[a-z]*\b|-a\b|--a[a-z]*\b)',
     "sudo with privilege flag (stdin/askpass/shell/list)"),
    # Combined short-flag form: -nS, -ns, -sa, -las — sudo flags packed
    # into a single -X token. Catches the same threat class.
    (r'\bsudo\b[^;|&\n]*?\s+-[a-z]*[sa][a-z]*\b',
     "sudo with combined-flag privilege escalation"),
]


# Pre-compiled variant (same rationale as HARDLINE_PATTERNS_COMPILED above).
DANGEROUS_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in DANGEROUS_PATTERNS
]


def _legacy_pattern_key(pattern: str) -> str:
    """Reproduce the old regex-derived approval key for backwards compatibility."""
    return pattern.split(r'\b')[1] if r'\b' in pattern else pattern[:20]


_PATTERN_KEY_ALIASES: dict[str, set[str]] = {}
for _pattern, _description in DANGEROUS_PATTERNS:
    _legacy_key = _legacy_pattern_key(_pattern)
    _canonical_key = _description
    _PATTERN_KEY_ALIASES.setdefault(_canonical_key, set()).update({_canonical_key, _legacy_key})
    _PATTERN_KEY_ALIASES.setdefault(_legacy_key, set()).update({_legacy_key, _canonical_key})

# Preserve approvals stored under the removed interpreter regex rules.
_REMOVED_PATTERN_KEY_ALIASES = {
    "script execution via -e/-c flag": "(python[23]?|perl|ruby|node)\\s+-[ec]\\s+",
    "script execution via heredoc": "(python[23]?|perl|ruby|node)\\s+<<",
}
for _canonical_key, _legacy_key in _REMOVED_PATTERN_KEY_ALIASES.items():
    _PATTERN_KEY_ALIASES.setdefault(_canonical_key, set()).update(
        {_canonical_key, _legacy_key}
    )
    _PATTERN_KEY_ALIASES.setdefault(_legacy_key, set()).update(
        {_legacy_key, _canonical_key}
    )


def _approval_key_aliases(pattern_key: str) -> set[str]:
    """Return all approval keys that should match this pattern.

    New approvals use the human-readable description string, but older
    command_allowlist entries and session approvals may still contain the
    historical regex-derived key.
    """
    return _PATTERN_KEY_ALIASES.get(pattern_key, {pattern_key})


# =========================================================================
# Detection
# =========================================================================

def _normalize_command_for_detection(command: str) -> str:
    """Normalize a command string before dangerous-pattern matching.

    Strips ANSI escape sequences (full ECMA-48 via tools.ansi_strip),
    null bytes, and normalizes Unicode fullwidth characters so that
    obfuscation techniques cannot bypass the pattern-based detection.
    """
    from tools.ansi_strip import strip_ansi

    # Strip all ANSI escape sequences (CSI, OSC, DCS, 8-bit C1, etc.)
    command = strip_ansi(command)
    # Strip null bytes
    command = command.replace('\x00', '')
    # Normalize Unicode (fullwidth Latin, halfwidth Katakana, etc.)
    command = unicodedata.normalize('NFKC', command)
    # Collapse shell line continuations (backslash-newline). The shell removes
    # BOTH characters and joins the tokens, so `rm -rf \<newline>/` executes as
    # `rm -rf /`. This must run BEFORE the generic backslash-escape strip below,
    # whose [^\n] class deliberately skips newlines and would otherwise leave
    # the dangling backslash wedged between tokens — defeating the structured
    # rm/mkfs/dd patterns (notably the HARDLINE root-delete floor, which cannot
    # be bypassed even with yolo). Handles both \n and \r\n line endings. Line
    # continuations carry no path separator, so this is a no-op on the Windows
    # home-prefix folds below (which match C:\Users\alice\... — no newline).
    command = re.sub(r'\\\r?\n', '', command)
    # Fold absolute home / active-profile-home prefixes into their canonical
    # ~/ and ~/.hermes/ forms so static user-sensitive patterns catch
    # /home/alice/.bashrc and C:\Users\alice\.bashrc the same way they catch
    # ~/.bashrc. Resolve at detection time (not via an import-time snapshot) so
    # it tracks HOME / HERMES_HOME even when those are set after this module is
    # imported — as the hermetic test conftest and profile/session launchers do.
    #
    # This MUST run before the backslash-escape strip below: on Windows the home
    # prefix is separated by backslashes (C:\Users\alice\...), which that strip
    # would otherwise dissolve (-> C:Usersalice) and make the fold impossible.
    # The fold matches either separator, so POSIX paths are unaffected by order.
    #
    # Fold the (more specific) Hermes home first: on Windows it nests under the
    # user home (C:\Users\alice\AppData\...\hermes), so folding the user home
    # first would eat the prefix the Hermes-home fold needs.
    command = _rewrite_resolved_hermes_home(command)
    command = _rewrite_resolved_user_home(command)
    # Strip shell backslash-escapes: r\m → rm. Prevents \-injection bypass.
    command = re.sub(r'\\([^\n])', r'\1', command)
    # Strip empty-string literals that split tokens: r''m → rm, r"\"m → rm.
    command = re.sub(r"''|\"\"", '', command)
    # Collapse $IFS / ${IFS} word-separator expansions to a literal space.
    # In any POSIX shell the IFS variable defaults to <space><tab><newline>,
    # so `rm${IFS}-rf${IFS}/` is executed as `rm -rf /`. Because the dangerous
    # and hardline patterns anchor on literal whitespace (\s) between a command
    # and its arguments, leaving the unexpanded `${IFS}` token in place lets an
    # attacker slip past EVERY pattern — including the unconditional hardline
    # floor (rm -rf /, mkfs, dd to raw device, shutdown/reboot). Substituting a
    # space here mirrors the shell's own expansion so the patterns fire. The
    # brace form also covers bash substring expansions like `${IFS:0:1}` (a
    # single space). Same de-obfuscation class as the backslash/empty-quote
    # handling above.
    command = re.sub(r'\$\{IFS\b[^}]*\}|\$IFS\b', ' ', command)
    return command
