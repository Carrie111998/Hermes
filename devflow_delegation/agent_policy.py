"""Safety policy for the DevFlow coding-agent runner.

Pure and side-effect free: no LLM calls, no filesystem writes. These primitives
exist because an LLM, unlike a fixed implementation command, is
non-deterministic — so the environment it sees, the work it may do, and the
content it produces all need explicit bounds.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Dict, Mapping, Optional, Sequence

# Names whose VALUES must never reach the agent or a child process.
_SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CRED", "AUTH")

# Deny-by-default: only these names survive scrubbing. Everything else is dropped,
# including innocuous-looking vars, because an allow-list cannot go stale unsafely.
_ENV_ALLOW = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "LANG", "LC_ALL", "TZ", "OS", "NUMBER_OF_PROCESSORS",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONPATH", "DDP_REQUEST_PATH",
})

_MIN_SECRET_LEN = 8

_SECRET_PATTERNS = (
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
)


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in _SECRET_NAME_MARKERS)


def scrubbed_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return a minimal environment for the agent and any child process.

    Deny-by-default against an explicit allow-list, and secret-shaped names are
    dropped even if they were somehow allow-listed.
    """
    source = dict(base if base is not None else {})
    env = {
        name: value for name, value in source.items()
        if name.upper() in _ENV_ALLOW and not _is_secret_name(name)
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def secret_values(base: Optional[Mapping[str, str]] = None) -> tuple[str, ...]:
    """Values of secret-shaped env vars, for later content scanning.

    Unlike ``scan_for_secrets``'s ``known_values`` filter (which guards against
    matching a trivially short substring in arbitrary text), any non-blank
    value of a secret-shaped name is captured here: the name already marks it
    as sensitive, so under-collecting risks missing a real credential later.
    """
    source = dict(base if base is not None else {})
    values = {
        str(value).strip() for name, value in source.items()
        if _is_secret_name(name) and str(value).strip()
    }
    return tuple(sorted(values))


def scan_for_secrets(text: str, *, known_values: Sequence[str] = ()) -> list[str]:
    """Return finding labels for credential material in ``text``. Empty means clean."""
    body = str(text or "")
    findings: list[str] = []
    for value in known_values:
        candidate = str(value or "").strip()
        if len(candidate) >= _MIN_SECRET_LEN and candidate in body:
            findings.append("known-credential-value")
            break
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(body):
            findings.append(label)
    return findings


class CeilingExceeded(RuntimeError):
    """A run ceiling was reached. Always fatal to the run."""


class Budget:
    """Tracks iterations, tokens and wall-clock against fail-closed ceilings."""

    def __init__(
        self,
        *,
        max_iterations: int,
        max_tokens: int,
        timeout_seconds: int,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds
        self._now = now
        # Recorded at construction time (distinct from `_started_at`, set by
        # `start()`) so callers can later observe the delay between building a
        # Budget and actually starting the clock on it.
        self._created_at = self._now()
        self._started_at = 0.0
        self.iterations = 0
        self.tokens = 0

    def start(self) -> None:
        self._started_at = self._now()

    def tick(self, tokens_used: int = 0) -> None:
        """Record one loop iteration. Raises CeilingExceeded on any breach."""
        self.iterations += 1
        self.tokens += max(0, int(tokens_used))
        if self.iterations > self._max_iterations:
            raise CeilingExceeded(f"iterations ceiling reached ({self._max_iterations})")
        if self.tokens > self._max_tokens:
            raise CeilingExceeded(f"tokens ceiling reached ({self._max_tokens})")
        if self._now() - self._started_at > self._timeout_seconds:
            raise CeilingExceeded(f"wall-clock ceiling reached ({self._timeout_seconds}s)")
