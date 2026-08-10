"""Shared path validation helpers for tool implementations.

Extracts the ``resolve() + relative_to()`` and ``..`` traversal check
patterns previously duplicated across skill_manager_tool, skills_tool,
skills_hub, cronjob_tools, and credential_files.
"""

import logging
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def validate_within_dir(path: Path, root: Path) -> Optional[str]:
    """Ensure *path* resolves to a location within *root*.

    Returns an error message string if validation fails, or ``None`` if the
    path is safe.  Uses ``Path.resolve()`` to follow symlinks and normalize
    ``..`` components.

    Usage::

        error = validate_within_dir(user_path, allowed_root)
        if error:
            return tool_error(error)
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except (ValueError, OSError) as exc:
        return f"Path escapes allowed directory: {exc}"
    return None


def has_traversal_component(path_str: str) -> bool:
    """Return True if *path_str* contains ``..`` traversal components.

    Quick check for obvious traversal attempts before doing full resolution.
    """
    parts = Path(path_str).parts
    return ".." in parts


# ---------------------------------------------------------------------------
# Filesystem-root detection (issue #82842)
# ---------------------------------------------------------------------------
#
# Hermes executed ``rd /s /q`` against the ``C:\\`` drive root after a
# user-approved scoped folder deletion (issue #82842). The destructive
# command's target resolved to ``C:\\`` after a multi-layer quote-escape
# collapse, so the approval flow saw the literal command but the *target*
# was a filesystem root. The hardline floor must reject root-target
# destructive commands *independent* of approval state — no yolo flag,
# no session allowlist, no smart-approval verdict can let ``rm -rf /``
# or ``rd /s /q C:\\`` run.
#
# ``is_filesystem_root`` classifies any path-like token as "this token
# resolves to a filesystem root" or "it does not". It covers POSIX
# (slash, repeated slashes, ``.``/``..`` collapse), Windows drive roots
# (``C:\\``, ``c:``, bare ``\\``) and UNC roots (``\\\\server\\share``),
# plus the ``\\\\?\\`` Windows device namespace.
#
# ``extract_filesystem_targets`` returns every path-like argument token
# that a destructive command actually carries so the approval layer can
# call ``is_filesystem_root`` on each token instead of reinventing the
# shell-tokenisation rules in every regex pattern.
#
# The implementations are deliberately conservative (fail-closed):
# ambiguous inputs default to "not a root" so the helper never causes a
# false-positive block on a legitimate non-root target. The hardline
# floor and softer approval layers still apply on top, so a permissive
# classification here does not weaken safety — it only means the
# root-target guard fires less often than it could.


# Explicit root forms that *must* be classified as filesystem roots.
# Listing them is safer than pattern-matching because the set is small,
# closed, and the consequences of a permissive classifier are limited
# (softer layers still apply).
_POSIX_FORMS_BARE_ROOT = {
    "",
    "/",
    "//",
    "///",
    "////",
    "/.",
    "/./",
    "/..",
    "/../",
    "/./..",
    "/../../..",
    "/../..",
    "/..//..",
    "/./..",
    "/.//",
    "/../",
    "/..",
}

# POSIX slash-only collapse forms — any number of ``/`` plus optional
# ``.`` / ``..`` segments between them collapse to the root.
import re as _re
_POSIX_SLASH_ONLY_RE = _re.compile(r"^/+(?:\.{1,2}/+)*\.{0,2}/?$")

_WINDOWS_DEVICE_ROOT_PREFIXES = ("\\\\?\\", "//?/")

_WINDOWS_BARE_ROOT_TOKENS = {
    "\\",
    "\\\\",
}


def is_filesystem_root(path: Optional[str]) -> bool:
    """Return True when *path* resolves to a filesystem root.

    Empty / ``None`` / relative inputs are treated as "not a root" (no
    path, no risk). The function never raises; ambiguous inputs are
    classified as "not a root" rather than raising so the calling
    approval layer stays fail-open to the rest of its checks.
    """
    if path is None:
        return False
    cleaned = path.strip()
    if not cleaned:
        return False

    # Windows device namespace / UNC device path: ``\\?\C:\`` (or
    # ``//?/C:/``) is the formal Windows root reference, treated here
    # as a filesystem root when its trailing segment is also root-ish.
    for prefix in _WINDOWS_DEVICE_ROOT_PREFIXES:
        if cleaned.startswith(prefix):
            rest = cleaned[len(prefix):].rstrip("\\/")
            if not rest:
                return True
            # ``\\?\`` alone (no drive) is also treated as a root.
            if rest in {"?", ""}:
                return True
            return _is_pure_drive_root(rest)

    # UNC root and "current drive root" via backslashes. ``\\server\share``
    # is a UNC root; ``\\`` (two backslashes) by itself names the root of
    # the current drive in cmd.exe and PowerShell.
    if cleaned.startswith("\\\\") or cleaned.startswith("//"):
        norm = cleaned.replace("\\", "/")
        # Bare ``\\`` / ``//`` (two char total) ⇒ current-drive root.
        if norm in ("\\\\", "//"):
            return True
        # Three-char ``\\?\`` / ``//?/`` is the device-namespace opener.
        if norm in ("\\\\?", "//?"):
            return True
        # Strip leading separators and inspect the segments.
        parts = [p for p in norm.strip("/").split("/") if p]
        # No segments at all (e.g. ``///`` or ``\\\\\\\\``) ⇒ already a
        # current-drive root under both POSIX and cmd semantics.
        if not parts:
            return True
        # ``\\server\share`` → ``["server", "share"]`` ⇒ UNC root.
        if len(parts) == 2:
            return True
        # Three-or-more backslash tokens (e.g. ``\\\\?\\UNC\\...``) are
        # device-namespace or extended-length paths; treat the opener
        # itself as root when nothing else follows it.
        if len(parts) == 1 and parts[0] == "?":
            return True
        return False

    # Windows drive-root forms: ``C:\``, ``c:``, ``C:\\.``, ``C:\..``.
    if _is_pure_drive_root(cleaned):
        return True

    # POSIX slash forms. ``/foo`` and ``/foo/bar`` are NOT roots; only
    # slash/segment collapse forms (``/``, ``//``, ``/.``, ``/..`` and
    # their combinations) count.
    forward = cleaned.replace("\\", "/")
    if forward in _POSIX_FORMS_BARE_ROOT:
        return True
    if _POSIX_SLASH_ONLY_RE.match(forward):
        return True
    if forward.lstrip("/") in {"", ".", ".."}:
        return True
    return False


def _is_pure_drive_root(candidate: str) -> bool:
    """Return True when *candidate* is a Windows drive-root spelling.

    Recognised:

    * ``C:\\`` / ``c:\\`` (the canonical drive-root anchor)
    * ``C:\\.`` / ``C:\\..`` (drive-root + a single ``.``/``..`` segment)
    * ``C:\\/`` (mixed-separator drive-root)
    * ``c:`` alone (drive-with-no-path is treated as the root of C:)

    Returns False when the candidate has any further subdirectory
    segment (so ``C:\\Users``, ``C:\\Users\\x`` are correctly rejected).
    """
    if not candidate or len(candidate) < 2:
        return False
    if candidate[1] != ":":
        return False
    drive = candidate[:2]
    if not ("A" <= drive[0].upper() <= "Z"):
        return False
    rest = candidate[2:]
    if not rest:
        return True
    # Drive root allows only leading separators followed by ``.`` or
    # ``..`` segments (which collapse back to the drive root under the
    # shell). Anything else is a subdirectory and not a root.
    rest_norm = rest.replace("\\", "/").strip("/")
    if not rest_norm:
        return True
    for part in rest_norm.split("/"):
        if part not in {"", ".", ".."}:
            return False
    return True


def extract_filesystem_targets(command: Optional[str]) -> list[str]:
    """Return path-like argument tokens found in *command*.

    Conservative: returns every token that *looks* like an absolute
    path — POSIX-leading (``/foo``), Windows-drive-leading (``C:\\foo``,
    ``C:``), or UNC-prefixed (``\\\\server\\share``). The calling
    approval layer classifies each returned token via
    :func:`is_filesystem_root`.

    The function never raises; malformed / empty input returns ``[]``.
    """
    if not command:
        return []
    tokens = _command_tokenize(command)
    targets: list[str] = []
    for token in tokens:
        if not token:
            continue
        stripped = token.strip().strip('"').strip("'")
        if not stripped:
            continue
        if _looks_like_filesystem_target(stripped):
            targets.append(stripped)
    # Defence in depth: shell quoting can collapse bare backslashes
    # into adjacent non-root tokens (issue #82842 echo reproduction),
    # so we *always* merge the fallback regex sweep regardless of
    # whether the token-stream path yielded targets. A drive-rooted
    # string that surfaces only in the fallback and is missed by
    # tokenize must still reach :func:`is_filesystem_root`.
    for fb in _fallback_extract_windows_paths(command):
        if fb not in targets:
            targets.append(fb)
    # Outer-quote wrap-around: when ``cmd /c "..."`` or
    # ``powershell -Command '...'`` collapses to a single outer
    # token, the inner structure of the wrapped command can still
    # contain a root-target destructive verb on a root path
    # containing backslashes (Windows UNC roots, device-namespace
    # roots). Round-8 MoA review flagged that these slipped through
    # the primary pass.
    #
    # Restricting the re-tokenize trigger to tokens that contain a
    # backslash (``\\``) — and not just any path separator — avoids
    # false positives such as ``git commit -m "rm -rf /"`` where the
    # quoted segment is *string data* (a commit message) but the
    # forward slash inside would otherwise be re-tokenized into a
    # root-target. Pure POSIX forward-slash paths are already
    # handled by the primary tokenizer + the drive-root fallback.
    for token in tokens:
        if not token:
            continue
        t = token.strip().strip('"').strip("'")
        if not t or "\\" not in t:
            continue
        for inner in _command_tokenize(t):
            inner = inner.strip().strip('"').strip("'")
            if not inner:
                continue
            if not _looks_like_filesystem_target(inner):
                continue
            if inner in targets:
                continue
            added_any = False
            for inner_fb in _fallback_extract_windows_paths(inner):
                if inner_fb not in targets:
                    targets.append(inner_fb)
                added_any = True
            if not added_any:
                targets.append(inner)
    # Deduplicate while preserving order.
    seen = set()
    out = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _looks_like_filesystem_target(token: str) -> bool:
    """Return True when *token* parses like an absolute path.

    Conservative: a token is a target if it has a leading slash, a
    Windows-drive anchor, or a UNC-style leading backslash pair. The
    helper also rejects short ``/x`` flag-like slash tokens that are
    too short to be a real path (``/c``, ``/s``, ``/q`` are all 2-char
    cmd-style flags in ``cmd /c del /s /q C:\\Users``).
    """
    if not token:
        return False

    # Short leading-slash tokens — typically Windows cmd flags.
    # ``/s`` (del recursive), ``/q`` (quiet), ``/c`` (carries command),
    # ``/f`` (force), ``/r`` (recursive), ``/a`` (attributes) — these are
    # not paths. A *bare* slash (``/`` or ``\\``) is a filesystem root,
    # so we accept it explicitly before the short-token heuristic.
    if token[:1] in ("/", "\\"):
        # Single-char slash is the POSIX / current-drive root.
        if len(token) == 1:
            return True
        # Repeated-separator slashes are also filesystem roots
        # (``//``, ``///``, ``\\\\``); these fail the cmd ``/c`` etc.
        # heuristic because they're all-separator with no alphanum.
        if all(c in ("/", "\\") for c in token):
            return True
        # Two-character slash tokens are cmd-style flags: ``/c``, ``/s``,
        # ``/q``, ``/f``, ``/r``, ``/a``, ``/k``, ``/d``. They are not
        # filesystem paths.
        if len(token) == 2 and token[1:].isalpha():
            return False
        if len(token) == 3 and token[1:2].isalpha() and token[2:3] not in ("/", "\\", "."):
            # ``/cfoo``-style flag prefix. Reject unless the suffix is a
            # separator, dot, or continuation segment.
            return False
        # Anything else that starts with a slash or backslash is treated
        # as a path-like token and let :func:`is_filesystem_root` decide.
        return True

    # Windows device-namespace paths.
    if token.startswith(_WINDOWS_DEVICE_ROOT_PREFIXES):
        return True

    # Windows drive-anchored paths: ``C:\\foo`` / ``C:`` / ``c:\\foo``.
    if (
        len(token) >= 2
        and token[1] == ":"
        and token[0].isalpha()
    ):
        return True

    # UNC-style opener: ``\\\\server\\share``.
    if token.startswith("\\\\"):
        return True

    return False


# Windows path extractors — used when shell quoting collapses bare
# backslashes into adjacent characters and a ``"\\""`` artifact surfaces.
_DRIVE_ROOT_RE = __import__("re").compile(r"[A-Za-z]:\\[A-Za-z0-9_. \\-]*")
_BACKSLASH_RE = __import__("re").compile(r"\\+")


def _fallback_extract_windows_paths(s: str) -> list[str]:
    """Best-effort extraction of Windows-looking paths and bare
    separators from *s*.

    Always merged into the regular token-stream output (not only on
    empty targets), because the failure mode from issue #82842 can
    surface bare-backslash roots embedded between regular tokens after
    bash → PowerShell → cmd quote-escape collapse. The function
    surfaces:

    * Drive-rooted substrings (``C:\\Users\\tester``) via
      ``_DRIVE_ROOT_RE``.
    * Bare backslash sequences that are NOT inside an already-matched
      drive path (``\\server\\share`` after collapse, ``\\`` alone).

    The returned list is deduplicated while preserving order so the
    downstream :func:`is_filesystem_root` classifier sees each
    candidate at most once.
    """
    out: list[str] = []
    # Drive-rooted absolute paths: ``C:\\Users\\tester`` etc.
    drive_matches = list(_DRIVE_ROOT_RE.findall(s))
    for m in drive_matches:
        if m not in out:
            out.append(m)
    drive_joined = " ".join(drive_matches)
    # Build the set of source-string spans covered by path-like
    # candidates. Drives come from _DRIVE_ROOT_RE; UNC and other
    # backslash-bearing tokens come from the primary tokenizer
    # stage. The tokenizer sometimes returns a token that wraps an
    # entire quoted body (when outer ``cmd /c "..."`` or
    # ``powershell -Command '...'`` collapse ate the inner structure)
    # — when such a token contains a backslash we still treat it as
    # a path-bearing span so the bare backslash inside it is NOT
    # re-emitted as a fresh root residue.
    spans: list[tuple[int, int]] = []
    cursor = 0
    for m in drive_matches:
        idx = s.find(m, cursor)
        if idx != -1:
            spans.append((idx, idx + len(m)))
            cursor = idx + len(m)
    cursor2 = 0
    for tok in _command_tokenize(s):
        idx = s.find(tok, cursor2)
        if idx == -1:
            continue
        # Register a span for a tokenizer-emitted token when either:
        # (a) the token is itself path-like (``_looks_like_filesystem_target``),
        # OR (b) the token contains a backslash — implying it wraps
        #     a quoted path-segment and its bare separators are NOT
        #     new bare-root residue from outer-quote collapse.
        if _looks_like_filesystem_target(tok) or ("\\" in tok or "/" in tok):
            spans.append((idx, idx + len(tok)))
        cursor2 = idx + len(tok)
    for bmatch in _BACKSLASH_RE.finditer(s):
        m = bmatch.group(0)
        if m in out:
            continue
        pos, end = bmatch.span()
        # Skip "\" inside any path-already-classified span.
        if any(sp <= pos and end <= ep for sp, ep in spans):
            continue
        # Skip the single-backslash continuation artefact
        # ``\\<whitespace>*<newline>`` — that is a shell line-continuation
        # token, not a filesystem-root target.
        if len(m) == 1:
            tail = s[pos + len(m):]
            stripped = tail.lstrip(" \t")
            if stripped.startswith("\n"):
                continue
            head = s[:pos].rstrip(" \t")
            if head.endswith((";", "&", "|")) and stripped and (stripped[0].isalnum() or stripped[0] == "_"):
                continue
        out.append(m)
    return out


def _command_tokenize(command: str) -> Iterable[str]:
    """Split *command* on shell separators without invoking a real shell.

    This is intentionally lightweight — the helper is used as a *defence
    in depth* by the approval layer, not as a complete shell parser.
    Single-quoted and double-quoted regions are preserved as a single
    token (with the quote characters stripped via
    :func:`extract_filesystem_targets`). Shell operators (``;``, ``&``,
    ``|``, ``&&``, ``||``, newline) terminate a token.
    """
    if not command:
        return []
    tokens: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if not in_double and ch == "'":
            in_single = not in_single
            i += 1
            continue
        if not in_single and ch == '"':
            in_double = not in_double
            i += 1
            continue
        if not in_single and not in_double:
            # Shell separators break a token. ``\\<newline>`` joins the
            # next line, so we have to skip line-continuations as well.
            if ch == "\\" and i + 1 < len(command) and command[i + 1] == "\n":
                i += 2
                continue
            if ch in (" ", "\t", "\n"):
                if buf:
                    tokens.append("".join(buf))
                    buf = []
                i += 1
                continue
            if ch in (";", "&", "|") and (
                not buf or buf[-1] not in {"&", "|"}
            ):
                # Operator boundary; do not capture the operator itself.
                if buf:
                    tokens.append("".join(buf))
                    buf = []
                i += 1
                while i < len(command) and command[i] in (" ", "\t"):
                    i += 1
                continue
        buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens
