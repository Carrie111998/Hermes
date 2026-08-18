r"""Known-value credential tripwire (Phase 9 / Packet C, layer 2).

DEFENSE IN DEPTH, NOT A BOUNDARY. Read that again before relying on it.

Layer 1 (agent/file_safety.is_credential_read_denied) is the boundary: it
refuses path-addressed reads of credential files. But `terminal` and
`execute_code` take a command/code string, not a declared path, so layer 1
cannot see them. This module covers those surfaces by recognising credential
values it has already seen on this machine and scrubbing them out of tool
results.

Why not inspect the command string instead? Because deciding whether a shell
command reads a path requires evaluating the shell. All of these reach .env
while containing nothing a path extractor would match:

    x=env; cat ~/.hermes/.$x
    cat ~/.hermes/.en''v
    cat "$(printf '%s' ~/.hermes/.env)"
    find ~ -name '.env' -exec cat {} \;

...while `grep -r TODO .` and `ls -la ~/.hermes` would false-positive. A
control that is both bypassable and annoying is worse than no control,
because it manufactures the belief that the surface is covered.

WHAT THIS CATCHES: verbatim emission. `cat .env`, `env | grep`, background
process logs, MCP file readers, execute_code printing os.environ.

WHAT IT DOES NOT CATCH: base64/hex/any encoding, reversal, chunked or partial
reads, values not present in a seeded location, and secrets that have since
been rotated. See SECURITY.md.

C2-REGRESSION NOTE (commit cd215c1ee4): that bug destroyed real tool params
because it triggered on a KEY NAME (`key`) applied to arguments and schemas
with structural recursion. This module triggers on none of those things. It
matches literal VALUES, only in result strings, with no recursion, and never
sees arguments or schemas. "Enter" cannot match: it fails the length guard.
"""

from __future__ import annotations

import logging
import os
import re
import string
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REDACTION_MARKER = "[REDACTED:CREDENTIAL]"

#: A value shorter than this is not a credential and is far too likely to be
#: an ordinary word. This single guard is what makes "Enter"/"Tab"/"Escape"
#: structurally unable to enter the seed set.
MIN_SECRET_LEN = 12
MIN_DISTINCT_CHARS = 6
MAX_SEED_VALUES = 500
MAX_SEED_FILE_BYTES = 256 * 1024

_VALUE_STOPLIST = frozenset({
    "changeme", "change_me", "placeholder", "your-api-key", "your_api_key",
    "yourapikeyhere", "your-token-here", "replace_me", "replaceme",
    "notasecret", "not-a-secret", "undefined", "development", "production",
    "localhost", "true", "false", "enabled", "disabled", "none", "null",
})

_PATHLIKE_RE = re.compile(r"^[~./]")
_HOSTPORT_RE = re.compile(r"^[\w.-]+:\d{1,5}$")
_PLACEHOLDER_RE = re.compile(r"^[<{\[(].*[>}\])]$|^\*+$|^x+$", re.IGNORECASE)
# Content types and similar `token/token` values: real strings that clear the
# entropy guards but are never credentials (e.g. "application/json").
_MIMETYPE_RE = re.compile(r"^[a-z][a-z0-9.+-]*/[a-z0-9.+-]+$", re.IGNORECASE)

# Variable names that mark a value as credential-bearing. Mirrors
# agent.redact._SECRET_ENV_NAMES.
_SECRET_NAME_RE = re.compile(
    r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE
)

_cache_generation: Optional[tuple] = None
_cache_values: frozenset = frozenset()
_cache_pattern: Optional[re.Pattern] = None


def is_scrubbable_secret_value(value: str) -> bool:
    """Return True if *value* is safe to treat as a scrubbable secret.

    Every guard here exists to keep a legitimate string out of the seed set.
    This is the narrowest possible trigger, and the direct answer to the
    C2 regression class.
    """
    if not isinstance(value, str):
        return False
    value = value.strip().strip("\"'")
    if len(value) < MIN_SECRET_LEN:
        return False                              # Enter, Tab, true, 8080
    if len(set(value)) < MIN_DISTINCT_CHARS:
        return False                              # aaaaaaaaaaaa, ------------
    if value.lower() in _VALUE_STOPLIST:
        return False                              # changeme, placeholder
    if _PATHLIKE_RE.match(value):
        return False                              # /usr/local/bin/python3
    if _HOSTPORT_RE.match(value):
        return False                              # localhost:5432
    if _PLACEHOLDER_RE.match(value):
        return False                              # <your-token>, ****
    if _MIMETYPE_RE.match(value):
        return False                              # application/json, text/html
    classes = sum([
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
        any(c in string.punctuation for c in value),
    ])
    if classes < 2:
        return False                              # abcdefghijkl
    return True


def _parse_env_values(text: str, *, all_values: bool = False) -> list[str]:
    """Extract candidate secret values from KEY=VALUE text.

    *all_values* controls the key-name filter, and the distinction matters:

      all_values=True   the source file is ITSELF a credential store (a .env,
                        ~/.aws/credentials, ...). Every value in it is a
                        secret by virtue of where it lives, whatever the key
                        is called. A key named PARTNER_HANDSHAKE_VALUE holds
                        just as real a credential as one named API_KEY, and
                        filtering on the name would silently miss it.

      all_values=False  the source is a general-purpose file (a shell rc) or
                        the process environment, where the overwhelming
                        majority of entries are innocuous (PATH, LANG, EDITOR).
                        Here the key name is the only signal available, so
                        seeding indiscriminately would flood the seed set with
                        ordinary strings.
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        name, _, value = line.partition("=")
        if not all_values and not _SECRET_NAME_RE.search(name.strip()):
            continue
        out.append(value.strip().strip("\"'"))
    return out


def _seed_paths() -> list[Path]:
    """Every path to seed from, credential-store and general-purpose alike."""
    from agent.file_safety import (
        build_credential_denied_paths,
        build_credential_denied_prefixes,
    )

    home = os.path.realpath(os.path.expanduser("~"))
    paths: list[Path] = [Path(p) for p in build_credential_denied_paths(home)]

    # Shell rc files are deliberately NOT read-denied (they are needed for
    # ordinary work), so seeding is their only coverage.
    for rc in (".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile"):
        paths.append(Path(home) / rc)

    for prefix in build_credential_denied_prefixes(home):
        d = Path(prefix)
        if d.is_dir():
            try:
                paths.extend(p for p in d.iterdir() if p.is_file())
            except OSError:
                continue

    # Top-level .env-family files in HERMES_HOME and the working directory.
    from agent.file_safety import is_credential_basename

    roots = []
    try:
        from hermes_constants import get_hermes_home
        roots.append(get_hermes_home())
    except Exception:
        pass
    roots.append(Path(os.environ.get("TERMINAL_CWD") or os.getcwd()))

    for root in roots:
        try:
            if root.is_dir():
                paths.extend(
                    p for p in root.iterdir()
                    if p.is_file() and is_credential_basename(p.name)
                )
        except OSError:
            continue
    return paths


def _generation_key() -> tuple:
    """Cheap fingerprint so the seed set rebuilds when a source changes."""
    parts = []
    for p in _seed_paths():
        try:
            st = p.stat()
            parts.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    env_names = tuple(sorted(
        k for k in os.environ if _SECRET_NAME_RE.search(k)
    ))
    env_fingerprint = tuple((k, hash(os.environ[k])) for k in env_names)
    return (tuple(sorted(parts)), env_fingerprint)


def _build_values() -> frozenset:
    values: set[str] = set()

    from agent.file_safety import is_credential_read_denied

    for path in _seed_paths():
        try:
            if not path.is_file() or path.stat().st_size > MAX_SEED_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # best-effort: seeding is fail-open, scanning is not
        # A file layer 1 already classifies as credential-bearing is a
        # credential store: take every value, not just secret-looking keys.
        all_values = is_credential_read_denied(str(path))
        for value in _parse_env_values(text, all_values=all_values):
            if is_scrubbable_secret_value(value):
                values.add(value)

    # Highest-value source: Hermes loads $HERMES_HOME/.env into its own
    # environment, so this covers `env`, `printenv`, and os.environ dumps
    # regardless of where the file lives.
    for name, value in os.environ.items():
        if _SECRET_NAME_RE.search(name) and is_scrubbable_secret_value(value):
            values.add(value)

    if len(values) > MAX_SEED_VALUES:
        logger.warning(
            "credential tripwire: seed set capped at %d (saw %d)",
            MAX_SEED_VALUES, len(values),
        )
        values = set(sorted(values, key=len, reverse=True)[:MAX_SEED_VALUES])
    return frozenset(values)


def _refresh() -> None:
    global _cache_generation, _cache_values, _cache_pattern
    generation = _generation_key()
    if generation == _cache_generation:
        return
    values = _build_values()
    _cache_generation = generation
    _cache_values = values
    # Longest first, so a secret that is a prefix of another masks as the
    # longer one.
    _cache_pattern = (
        re.compile("|".join(re.escape(v) for v in sorted(values, key=len, reverse=True)))
        if values else None
    )


def known_secret_count() -> int:
    """Return how many values are seeded. Safe to log and to assert on."""
    _refresh()
    return len(_cache_values)


def is_value_seeded(value: str) -> bool:
    """Return whether a SPECIFIC value is in the seed set.

    This is the only supported way to interrogate seeding. There is
    deliberately no accessor that returns the seed set itself: any such
    function puts every credential on the machine one `print()`, one log
    line, or one failed test assertion away from disclosure. That is not
    hypothetical -- an assertion against a set-returning accessor is exactly
    how real credentials were first leaked into an agent transcript during
    this packet's own development.
    """
    _refresh()
    return value in _cache_values


def scrub_known_secrets(text: str) -> tuple[str, int]:
    """Replace every known secret value in *text*. Returns (text, hit_count)."""
    if not text or not isinstance(text, str):
        return text, 0
    _refresh()
    if _cache_pattern is None:
        return text, 0
    hits = 0

    def _sub(_m):
        nonlocal hits
        hits += 1
        return REDACTION_MARKER

    return _cache_pattern.sub(_sub, text), hits


def contains_known_secret(text: str) -> bool:
    """Return True if *text* contains any known secret value."""
    if not text or not isinstance(text, str):
        return False
    _refresh()
    return bool(_cache_pattern and _cache_pattern.search(text))


def reset_cache() -> None:
    """Drop the cached seed set. For tests."""
    global _cache_generation, _cache_values, _cache_pattern
    _cache_generation = None
    _cache_values = frozenset()
    _cache_pattern = None
